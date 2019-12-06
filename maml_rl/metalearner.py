import torch
import asyncio

from torch.nn.utils.convert_parameters import (vector_to_parameters,
                                               parameters_to_vector)
from torch.distributions.kl import kl_divergence

from maml_rl.samplers import MultiTaskSampler
from maml_rl.utils.torch_utils import weighted_mean, detach_distribution
from maml_rl.utils.optimization import conjugate_gradient
from maml_rl.utils.reinforcement_learning import reinforce_loss


class ModelAgnosticMetaLearning(object):
    def __init__(self,
                 sampler,
                 policy,
                 fast_lr=0.5,
                 num_steps=1,
                 gamma=0.95,
                 tau=1.0,
                 first_order=False,
                 device='cpu'):
        self.sampler = sampler
        self.fast_lr = fast_lr
        self.num_steps = num_steps
        self.gamma = gamma
        self.tau = tau
        self.first_order = first_order
        self.device = torch.device(device)

        self.policy = policy
        self.policy.to(self.device)

        if isinstance(sampler, MultiTaskSampler):
            self._event_loop = self.sampler._event_loop
        else:
            self._event_loop = asyncio.get_event_loop()

    def adapt(self, episodes):
        params = None
        for _ in range(self.num_steps):
            inner_loss = reinforce_loss(self.policy, episodes, params=params)
            params = self.policy.update_params(inner_loss,
                                               params=params,
                                               step_size=self.fast_lr,
                                               first_order=self.first_order)
        return params

    def sample_async(self, tasks):
        return self.sampler.sample_async(tasks,
                                         num_steps=self.num_steps,
                                         fast_lr=self.fast_lr,
                                         gamma=self.gamma,
                                         tau=self.tau,
                                         device=self.device.type)

    def sample(self, tasks):
        return self.sampler.sample(tasks,
                                   num_steps=self.num_steps,
                                   fast_lr=self.fast_lr,
                                   gamma=self.gamma,
                                   tau=self.tau,
                                   device=self.device.type)

    def hessian_vector_product(self, kl, damping=1e-2):
        grads = torch.autograd.grad(kl,
                                    self.policy.parameters(),
                                    create_graph=True)
        def _product(vector):
            flat_grad_kl = parameters_to_vector(grads)

            grad_kl_v = torch.dot(flat_grad_kl, vector)
            grad2s = torch.autograd.grad(grad_kl_v,
                                         self.policy.parameters(),
                                         retain_graph=True)
            flat_grad2_kl = parameters_to_vector(grad2s)

            return flat_grad2_kl + damping * vector
        return _product

    async def surrogate_loss(self,
                             train_futures,
                             valid_futures,
                             params=None,
                             old_pi=None):
        if params is None:
            params = self.adapt(await train_futures)

        with torch.set_grad_enabled(old_pi is None):
            valid_episodes = await valid_futures
            pi = self.policy(valid_episodes.observations, params=params)

            if old_pi is None:
                old_pi = detach_distribution(pi)

            log_ratio = (pi.log_prob(valid_episodes.actions)
                         - old_pi.log_prob(valid_episodes.actions))
            if log_ratio.dim() > 2:
                log_ratio = torch.sum(log_ratio, dim=2)
            ratio = torch.exp(log_ratio)

            loss = -weighted_mean(ratio * valid_episodes.advantages,
                                  dim=0,
                                  weights=valid_episodes.mask)

            mask = valid_episodes.mask
            if valid_episodes.actions.dim() > 2:
                mask = mask.unsqueeze(dim=2)
            kl = weighted_mean(kl_divergence(pi, old_pi),
                               dim=0,
                               weights=mask)

        return loss, kl, params, old_pi

    def step(self,
             train_episodes,
             valid_episodes,
             max_kl=1e-3,
             cg_iters=10,
             cg_damping=1e-2,
             ls_max_steps=10,
             ls_backtrack_ratio=0.5):
        num_tasks = len(train_episodes)

        # Compute the surrogate loss
        coroutine = asyncio.gather(*[self.surrogate_loss(train,
                                                         valid,
                                                         params=None,
                                                         old_pi=None)
            for (train, valid) in zip(train_episodes, valid_episodes)])
        losses, kls, parameters, old_pis = zip(
            *self._event_loop.run_until_complete(coroutine))

        old_loss = sum(losses) / num_tasks
        grads = torch.autograd.grad(old_loss,
                                    self.policy.parameters(),
                                    retain_graph=True)
        grads = parameters_to_vector(grads)

        # Compute the step direction with Conjugate Gradient
        kl = sum(kls) / num_tasks
        hessian_vector_product = self.hessian_vector_product(kl,
                                                             damping=cg_damping)
        stepdir = conjugate_gradient(hessian_vector_product,
                                     grads,
                                     cg_iters=cg_iters)

        # Compute the Lagrange multiplier
        shs = 0.5 * torch.dot(stepdir, hessian_vector_product(stepdir))
        lagrange_multiplier = torch.sqrt(shs / max_kl)

        step = stepdir / lagrange_multiplier

        # Save the old parameters
        old_params = parameters_to_vector(self.policy.parameters())

        # Line search
        step_size = 1.0
        for _ in range(ls_max_steps):
            vector_to_parameters(old_params - step_size * step,
                                 self.policy.parameters())

            coroutine = asyncio.gather(*[self.surrogate_loss(train,
                                                             valid,
                                                             params=params,
                                                             old_pi=old_pi)
                for (train, valid, params, old_pi)
                in zip(train_episodes, valid_episodes, parameters, old_pis)])

            losses, kls, _, _ = zip(
                *self._event_loop.run_until_complete(coroutine))
            improve = (sum(losses) / num_tasks) - old_loss
            kl = sum(kls) / num_tasks
            if (improve.item() < 0.0) and (kl.item() < max_kl):
                break
            step_size *= ls_backtrack_ratio
        else:
            vector_to_parameters(old_params, self.policy.parameters())

        if isinstance(self.sampler, MultiTaskSampler):
            self.sampler._join_consumer_threads()

    def close(self):
        self.sampler.close()


MAMLTRPO = ModelAgnosticMetaLearning

"""
A training script of Advantage-Weighted Actor-Critic on OpenAI Gym Mujoco environments.

This script follows the settings of https://arxiv.org/abs/2006.09359 as much
as possible.
Currently, only online RL is supported.
"""
from distutils.version import LooseVersion
import functools
import logging
import os

import torch
from torch import nn
from torch import distributions
import gym
import gym.wrappers
import hydra
from omegaconf import DictConfig
import numpy as np

import pfrlx.experiments as experiments
from pfrlx.networks.lmbda import Lambda
import pfrlx.utils as utils
import pfrlx.replay_buffers as replay_buffers
import pfrlx.envs as envs
import pfrlx.wrappers as wrappers
import pfrlx.networks as networks
import pfrlx.algos as algos
import pfrlx.policies as policies


@hydra.main(config_path='../conf/awac/default.yaml')
def main(conf: DictConfig):
    logging.basicConfig(level=conf.log.level)
    experiments.prepare_output_dir(args={}, basedir=os.getcwd(), exp_id='')
    print(f'Output files are saved in {os.getcwd()}')

    # Set a random seed used in PFRL
    utils.set_random_seed(conf.seed)

    # Set different random seeds for different subprocesses.
    # If seed=0 and processes=4, subprocess seeds are [0, 1, 2, 3].
    # If seed=1 and processes=4, subprocess seeds are [4, 5, 6, 7].
    process_seeds = np.arange(conf.n_envs) + conf.seed * conf.n_envs
    assert process_seeds.max() < 2 ** 32

    def make_dmc_mujoco_env(process_idx, test):
        if conf.dmc:
            return make_dmc_env(process_idx, test)
        else:
            return make_env(process_idx, test)

    def make_dmc_env(process_idx, test):
        import dmc2gym
        env = dmc2gym.make(
            domain_name=conf.domain, task_name=conf.task
        )
        # Unwrap TimeLimit wrapper
        assert isinstance(env, gym.wrappers.TimeLimit)
        env = env.env
        # Use different random seeds for train and test envs
        process_seed = int(process_seeds[process_idx])
        env_seed = 2 ** 32 - 1 - process_seed if test else process_seed
        env.seed(env_seed)
        # Cast observations to float32 because our model uses float32
        env = wrappers.CastObservationToFloat32(env)
        # Normalize action space to [-1, 1]^n
        env = wrappers.NormalizeActionSpace(env)
        if conf.log.monitor:
            env = gym.wrappers.Monitor(env, os.getcwd())
        if conf.log.render:
            env = wrappers.Render(env)
        return env

    def make_env(process_idx, test):
        env = gym.make(conf.env)
        # Unwrap TimeLimit wrapper
        assert isinstance(env, gym.wrappers.TimeLimit)
        env = env.env
        # Use different random seeds for train and test envs
        process_seed = int(process_seeds[process_idx])
        env_seed = 2 ** 32 - 1 - process_seed if test else process_seed
        env.seed(env_seed)
        # Cast observations to float32 because our model uses float32
        env = wrappers.CastObservationToFloat32(env)
        # Normalize action space to [-1, 1]^n
        env = wrappers.NormalizeActionSpace(env)
        if conf.log.monitor:
            env = gym.wrappers.Monitor(env, os.getcwd())
        if conf.log.render:
            env = wrappers.Render(env)
        return env

    def make_batch_env(test):
        return envs.MultiprocessVectorEnv(
            [
                functools.partial(make_dmc_mujoco_env, idx, test)
                for idx, env in enumerate(range(conf.n_envs))
            ]
        )

    sample_env = make_dmc_mujoco_env(process_idx=0, test=False)
    timestep_limit = sample_env.spec.max_episode_steps
    obs_space = sample_env.observation_space
    action_space = sample_env.action_space
    print("Observation space:", obs_space)
    print("Action space:", action_space)

    obs_size = obs_space.low.size
    action_size = action_space.low.size

    if LooseVersion(torch.__version__) < LooseVersion("1.5.0"):
        raise Exception("This script requires a PyTorch version >= 1.5.0")

    # select explorer
    explorer = policies.GaussianHeadWithStateIndependentCovariance(action_size, var_type="diagonal")

    policy = nn.Sequential(
        nn.Linear(obs_size, conf.actor.nn_size),
        nn.ReLU(),
        nn.Linear(conf.actor.nn_size, conf.actor.nn_size),
        nn.ReLU(),
        nn.Linear(conf.actor.nn_size, action_size),
        nn.Tanh(),
        explorer,
    )
    torch.nn.init.xavier_uniform_(policy[0].weight)
    torch.nn.init.xavier_uniform_(policy[2].weight)
    torch.nn.init.xavier_uniform_(policy[4].weight, gain=conf.actor.output_scale)
    policy_optimizer = torch.optim.Adam(
        policy.parameters(),
        lr=conf.actor.lr,
        weight_decay=conf.actor.weight_decay
    )

    def make_q_func_with_optimizer():
        q_func = nn.Sequential(
            networks.ConcatObsAndAction(),
            nn.Linear(obs_size + action_size, conf.critic.nn_size),
            nn.ReLU(),
            nn.Linear(conf.critic.nn_size, conf.critic.nn_size),
            nn.ReLU(),
            nn.Linear(conf.critic.nn_size, 1),
        )
        torch.nn.init.xavier_uniform_(q_func[1].weight)
        torch.nn.init.xavier_uniform_(q_func[3].weight)
        torch.nn.init.xavier_uniform_(q_func[5].weight)
        q_func_optimizer = torch.optim.Adam(
            q_func.parameters(),
            lr=conf.critic.lr,
            weight_decay=conf.actor.weight_decay
        )
        return q_func, q_func_optimizer

    q_func1, q_func1_optimizer = make_q_func_with_optimizer()
    q_func2, q_func2_optimizer = make_q_func_with_optimizer()

    rbuf = replay_buffers.ReplayBuffer(conf.replay_buffer.size)

    def burnin_action_func():
        """Select random actions until model is updated one or more times."""
        return np.random.uniform(action_space.low, action_space.high).astype(np.float32)

    agent = algos.AWAC(
        conf,
        policy,
        q_func1,
        q_func2,
        policy_optimizer,
        q_func1_optimizer,
        q_func2_optimizer,
        rbuf,
        burnin_action_func=burnin_action_func,
    )

    if conf.pretrained.dir or conf.pretrained.load:
        if conf.pretrained.load:
            raise Exception("Pretrained models are currently unsupported.")
        # either dir or load must be false
        assert not (conf.pretrained.dir and conf.pretrained.load)
        if conf.pretrained.dir:
            agent.load(conf.pretrained.dir)
        else:
            agent.load(
                utils.download_model(
                    "AWAC", conf.env, model_type="final"
                )[0]
            )

    if conf.demo:
        env = make_batch_env(True)
        eval_stats = experiments.eval_performance(
            env=env,
            agent=agent,
            n_steps=None,
            n_episodes=conf.eval.n_runs,
            max_episode_len=timestep_limit,
        )
        print(
            "n_runs: {} mean: {} median: {} stdev {}".format(
                conf.eval.n_runs,
                eval_stats["mean"],
                eval_stats["median"],
                eval_stats["stdev"],
            )
        )
    else:
        experiments.train_agent_batch_with_evaluation(
            agent=agent,
            env=make_batch_env(test=False),
            eval_env=make_batch_env(test=True),
            outdir=os.getcwd(),
            steps=conf.steps,
            eval_n_steps=None,
            eval_n_episodes=conf.eval.n_runs,
            eval_interval=conf.eval.interval,
            log_interval=conf.log.interval,
            max_episode_len=timestep_limit
        )


if __name__ == "__main__":
    main()

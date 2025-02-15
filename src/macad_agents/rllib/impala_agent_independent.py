import os
import argparse

from gym.spaces import Box, Discrete
import ray
from ray.rllib.agents.impala import impala
from ray.rllib.models.preprocessors import Preprocessor
from ray.rllib.models.catalog import ModelCatalog
import ray.tune as tune
from ray.tune import register_env
from ray.rllib.agents.impala.vtrace_policy_graph import VTracePolicyGraph

# from env.carla.multi_env import MultiCarlaEnv
# from env.envs.intersection.urban_2_car_1_ped import \
# UrbanSignalIntersection2Car1Ped1Bike
# from env.envs.urban_2car import Urban2Car
# from env.envs.intersection.urban_signal_intersection_3c import \
#    UrbanSignalIntersection3Car
from macad_gym.envs.intersection.stop_sign_urban_intersection_3c import \
    StopSignUrbanIntersection3Car, SSUI3C_CONFIGS
from macad_agents.rllib.models import register_mnih15_net
from macad_agents.rllib.env_wrappers import wrap_deepmind

parser = argparse.ArgumentParser()
parser.add_argument(
    "--env",
    default="PongNoFrameskip-v4",
    help="Name Gym env. Used only in debug mode. Default=PongNoFrameskip-v4")
parser.add_argument(
    "--checkpoint-path",
    default=None,
    help="Path to checkpoint to resume training")
parser.add_argument(
    "--disable-comet",
    action="store_true",
    help="Disables comet logging. Used for local smoke tests")
parser.add_argument(
    "--num-workers",
    default=2,
    type=int,
    help="Num workers (CPU cores) to use")
parser.add_argument(
    "--num-gpus", default=1, type=int, help="Number of gpus to use. Default=2")
parser.add_argument(
    "--sample-bs-per-worker",
    default=50,
    type=int,
    help="Number of samples in a batch per worker. Default=50")
parser.add_argument(
    "--train-bs",
    default=4,
    type=int,
    help="Train batch size. Use as per available GPU mem. Default=500")
parser.add_argument(
    "--envs-per-worker",
    default=1,
    type=int,
    help="Number of env instances per worker. Default=10")
parser.add_argument(
    "--notes",
    default=None,
    help="Custom experiment description to be added to comet logs")
parser.add_argument(
    "--model-arch",
    default="mnih15",
    help="Model architecture to use. Default=mnih15")
parser.add_argument(
    "--num-steps",
    default=2000000,
    type=int,
    help="Number of steps to train. Default=20M")
parser.add_argument(
    "--num-iters",
    default=20,
    type=int,
    help="Number of training iterations. Default=20")
parser.add_argument(
    "--log-graph",
    action="store_true",
    help="Write TF graph on Tensorboard for debugging")
parser.add_argument(
    "--num-framestack",
    type=int,
    default=4,
    help="Number of obs frames to stack")
parser.add_argument(
    "--debug", action="store_true", help="Run in debug-friendly mode")
parser.add_argument(
    "--redis-address",
    default=None,
    help="Address of ray head node. Be sure to start ray with"
    "ray start --redis-address <...> --num-gpus<.> before running this script")
parser.add_argument(
    "--use-lstm", action="store_true", help="Append a LSTM cell to the model")

args = parser.parse_args()

model_name = args.model_arch
if model_name == "mnih15":
    register_mnih15_net()  # Registers mnih15
else:
    print("Unsupported model arch. Using default")
    register_mnih15_net()
    model_name = "mnih15"

# Used only in debug mode
env_name = "SSUI3CCARLA"

num_framestack = args.num_framestack

env_actor_configs = SSUI3C_CONFIGS

# env_config["env"]["render"] = False


def env_creator(env_config):
    # NOTES: env_config.worker_index & vector_index are useful for
    # curriculum learning or joint training experiments
    # env = MultiCarlaEnv(env_config)  # (env_actor_configs)
    env = StopSignUrbanIntersection3Car()  # Urban2Car()
    # Apply wrappers to: convert to Grayscale, resize to 84 x 84,
    # stack frames & some more op
    env = wrap_deepmind(env, dim=84, num_framestack=num_framestack)
    return env


register_env(env_name, lambda config: env_creator(config))


# Placeholder to enable use of a custom pre-processor
class ImagePreproc(Preprocessor):
    def _init_shape(self, obs_space, options):
        shape = (84, 84, 3)  # Adjust third dim if stacking frames
        return shape
        # return gym.spaces.Box(
        #    low=0.0, high=1.0, shape=self.shape)

    def transform(self, observation):
        return observation


ModelCatalog.register_custom_preprocessor("sq_im_84", ImagePreproc)

# NOTE: The config def here is unused. See agent init args. Config is created
# during agent init. The below config is just a placeholder for future quick
# experiments.
config = {
    # Model and preprocessor options.
    "model": {
        "custom_model": model_name,
        "custom_options": {
            # Custom notes for the experiment
            "notes": {
                "args": vars(args)
            },
        },
        # NOTE:Wrappers are applied by RLlib if custom_preproc is NOT specified
        "custom_preprocessor": "sq_im_84",
        "dim": 84,
        "free_log_std": False,  # if args.discrete_actions else True,
        "grayscale": True,
        # conv_filters to be used with the custom CNN model.
        # "conv_filters": [[16, [4, 4], 2], [32, [3, 3], 2], [16, [3, 3], 2]]
    },
    # preproc_pref is ignored if custom_preproc is specified
    # "preprocessor_pref": "deepmind",

    # env_config to be passed to env_creator
    "env_config": env_actor_configs
}
# Common Agent config
config.update({
    # Discount factor of the MDP
    "gamma": 0.99,
    # Number of steps after which the rollout gets cut
    "horizon": None,
    # Whether to rollout "complete_episodes" or "truncate_episodes"
    "batch_mode": "truncate_episodes",
    # Whether to use a background thread for sampling (slightly off-policy)
    "sample_async": False,
    # Which observation filter to apply to the observation
    "observation_filter": "NoFilter",
    # Whether to LZ4 compress observations
    "compress_observations": False,
    "num_gpus": args.num_gpus
})
# Impala specific config
# From Appendix G in https://arxiv.org/pdf/1802.01561.pdf
config.update({
    # V-trace params (see vtrace.py).
    "vtrace":
    True,
    "vtrace_clip_rho_threshold":
    1.0,
    "vtrace_clip_pg_rho_threshold":
    1.0,

    # System params.
    # Should be divisible by num_envs_per_worker
    "sample_batch_size":
    args.sample_bs_per_worker,
    "train_batch_size":
    args.train_bs,
    "min_iter_time_s":
    10,
    "num_workers":
    args.num_workers,
    # Number of environments to evaluate vectorwise per worker.
    "num_envs_per_worker":
    args.envs_per_worker,
    "num_cpus_per_worker":
    1,
    "num_gpus_per_worker":
    1,

    # Learning params.
    "grad_clip":
    40.0,
    "clip_rewards":
    True,
    # either "adam" or "rmsprop"
    "opt_type":
    "adam",
    "lr":
    6e-4,
    "lr_schedule": [
        [0, 0.0006],
        [20000000, 0.000000000001],  # Anneal linearly to 0 from start 2 end
    ],
    # rmsprop considered
    "decay":
    0.99,
    "momentum":
    0.0,
    "epsilon":
    0.1,
    # balancing the three losses
    "vf_loss_coeff":
    0.5,  # Baseline loss scaling
    "entropy_coeff":
    -0.01,
})

# config["env"] = tune.grid_search(["dm-" + env_id for env_id in env_names])
# config["env"] = tune.grid_search([env_name for env_name in env_names])

if args.redis_address is not None:
    # num_gpus (& num_cpus) must not be provided when connecting to an
    # existing cluster
    ray.init(redis_address=args.redis_address)
else:
    ray.init(num_gpus=args.num_gpus)


def default_policy():
    return (VTracePolicyGraph, Box(0.0, 255.0, shape=(84, 84, 3)), Discrete(9),
            {
                "gamma": 0.99,
                "use_lstm": args.use_lstm
            })


# Create a debugging friendly instance
if args.debug:
    from tqdm import tqdm
    from pprint import pprint
    trainer = impala.ImpalaAgent(
        env="dm-" + env_name,
        # Use independent policy graphs for each agent
        config={
            "multiagent": {
                "policy_graphs": {
                    id: default_policy()
                    for id in env_actor_configs["actors"].keys()
                },
                "policy_mapping_fn": lambda agent_id: agent_id,
            },
            "env_config": env_actor_configs,
            "num_workers": args.num_workers,
            "num_envs_per_worker": args.envs_per_worker,
            "sample_batch_size": args.sample_bs_per_worker,
            "train_batch_size": args.train_bs
        })
    if args.checkpoint_path and os.path.isfile(args.checkpoint_path):
        trainer.restore(args.checkpoint_path)
        print("Loaded checkpoint from:{}".format(args.checkpoint_path))

    for iter in tqdm(range(args.num_iters), desc="Iters"):
        results = trainer.train()
        if iter % 500 == 0:
            trainer.save("saved_models/multi-carla/" + args.model_arch)
        pprint(results)
else:
    # Unused exp_spec
    experiment_spec = tune.Experiment(
        "multi-carla/" + args.model_arch,
        "IMPALA",
        # timesteps_total is init with None (not 0) which causes issue
        # stop={"timesteps_total": args.num_steps},
        stop={"timesteps_since_restore": args.num_steps},
        config=config,
        checkpoint_freq=1000,
        checkpoint_at_end=True,
        resources_per_trial={
            "cpu": 4,
            "gpu": 1
        })

    tune.run_experiments({
        "MA-Inde-IMPALA-SSUI3CCARLA": {
            "run": "IMPALA",
            "env": env_name,
            "stop": {
                "training_iteration": args.num_iters
            },
            "config": {
                "log_level": "DEBUG",
                "num_sgd_iter": 10,  # Enables Experience Replay
                "multiagent": {
                    "policy_graphs": {
                        id: default_policy()
                        for id in env_actor_configs["actors"].keys()
                    },
                    "policy_mapping_fn":
                    tune.function(lambda agent_id: agent_id),
                },
                "env_config": env_actor_configs,
                "num_workers": args.num_workers,
                "num_envs_per_worker": args.envs_per_worker,
                "sample_batch_size": args.sample_bs_per_worker,
                "train_batch_size": args.train_bs
            },
            "checkpoint_freq": 500,
            "checkpoint_at_end": True,
        }
    })

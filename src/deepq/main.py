if __name__ == '__main__':
    import os,sys,inspect
    currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    parentdir = os.path.dirname(currentdir)
    sys.path.insert(0,parentdir)

import deepq.experiment
from common.train_wrappers import wrap
import gym
import gym_maze
import deepq.catch_experiment

if __name__ == '__main__':
    total_steps = 1000000
    trainer = deepq.experiment.DeepQTrainer(
        env_kwargs = dict(id='Catch-v0'), 
        model_kwargs = dict(action_space_size = 3),
        annealing_steps = total_steps // 10,
        max_episode_steps = None)

    trainer = wrap(trainer, max_time_steps=total_steps, episode_log_interval=10)
    trainer.run()

else:
    raise('This script cannot be imported')
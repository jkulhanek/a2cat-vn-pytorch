# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np


# weight initialization based on muupan's code
# https://github.com/muupan/async-rl/blob/master/a3c_ale.py
def fc_initializer(input_channels, dtype=tf.float32):
  def _initializer(shape, dtype=dtype, partition_info=None):
    d = 1.0 / np.sqrt(input_channels)
    return tf.random_uniform(shape, minval=-d, maxval=d)
  return _initializer


def conv_initializer(kernel_width, kernel_height, input_channels, dtype=tf.float32):
  def _initializer(shape, dtype=dtype, partition_info=None):
    d = 1.0 / np.sqrt(input_channels * kernel_width * kernel_height)
    return tf.random_uniform(shape, minval=-d, maxval=d)
  return _initializer


class UnrealModel(object):
  """
  UNREAL algorithm network model.
  """
  def __init__(self,
               action_space_size,
               thread_index, # -1 for global
               use_lstm,
               use_pixel_change,
               use_value_replay,
               use_reward_prediction,
               use_goal_input,              
               pixel_change_lambda,
               entropy_beta,
               device,
               use_deepq_network = False,
               for_display=False,
               stack_last_frames = None):
    self._device = device
    self._action_size = action_space_size
    self._thread_index = thread_index
    self._use_lstm = use_lstm
    self._use_pixel_change = use_pixel_change
    self._use_value_replay = use_value_replay
    self._use_reward_prediction = use_reward_prediction
    self._use_goal_input = use_goal_input
    self._pixel_change_lambda = pixel_change_lambda
    self._entropy_beta = entropy_beta
    self._use_deepq_network = use_deepq_network
    self._image_shape = [84,84] # Note much of network parameters are hard coded so if we change image shape, other parameters will need to change

    self._create_network(for_display)
    self.settings = dict(
      action_space_size = self._action_size,

    )
    
  def _create_network(self, for_display):
    scope_name = "net_{0}".format(self._thread_index)
    with tf.device(self._device), tf.variable_scope(scope_name) as scope:
      # lstm
      self.lstm_cell = tf.nn.rnn_cell.LSTMCell(256, state_is_tuple=True, name='basic_lstm_cell')
      
      # [base A3C network]
      self._create_base_network()

      # [Pixel change network]
      if self._use_pixel_change:
        self._create_pc_network()
        if for_display:
          self._create_pc_network_for_display()

      # [Value replay network]
      if self._use_value_replay:
        self._create_vr_network()

      # [Reward prediction network]
      if self._use_reward_prediction:
        self._create_rp_network()

      if self._use_deepq_network:
        self._deepq()
      
      self.reset_state()

      self.variables = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope_name)


  def _create_base_network(self):
    # State (Base image input)
    self.base_input = tf.placeholder("float", [None, self._image_shape[0], self._image_shape[1], 3], name='base_input')

    # Last action and reward
    self.base_last_action_reward_input = tf.placeholder("float", [None, self._action_size+1])

    if self._use_goal_input:
      self.goal_input = tf.placeholder("float", [None, self._image_shape[0], self._image_shape[1], 3], name='goal_input')
    else:
      self.goal_input = None


    # Conv layers
    base_conv_output = self._base_conv_layers(self.base_input, self.goal_input)

    
    if self._use_lstm:
      # LSTM layer
      self.base_initial_lstm_state0 = tf.placeholder(tf.float32, [1, 256], name='base_initial_lstm_state0')
      self.base_initial_lstm_state1 = tf.placeholder(tf.float32, [1, 256], name='base_initial_lstm_state1')

      self.base_initial_lstm_state = tf.contrib.rnn.LSTMStateTuple(self.base_initial_lstm_state0,
                                                                   self.base_initial_lstm_state1)

      self.base_lstm_outputs, self.base_lstm_state = \
        self._base_lstm_layer(base_conv_output,
                              self.base_last_action_reward_input,
                              self.base_initial_lstm_state)

      (self.base_pi_without_softmax, self.base_pi) = self._base_policy_layer(self.base_lstm_outputs) # policy output
      self.base_v  = self._base_value_layer(self.base_lstm_outputs)  # value output
    else:
      self.base_fcn_outputs = self._base_fcn_layer(base_conv_output,
                                                   self.base_last_action_reward_input)
      (self.base_pi_without_softmax, self.base_pi) = self._base_policy_layer(self.base_fcn_outputs) # policy output
      self.base_v  = self._base_value_layer(self.base_fcn_outputs)  # value output

    
  def _base_conv_layers(self, state_input, goal_input = None, reuse=False, name = "base_conv"):
    with tf.variable_scope(name, reuse=reuse) as scope:
      # Weights
      W_conv1, b_conv1 = self._conv_variable([8, 8, 3, 16],  "base_conv1") # 16 8x8 filters
      W_conv2, b_conv2 = self._conv_variable([4, 4, 32 if goal_input is not None else 16, 32], "base_conv2") # 32 4x4 filters

      # Nodes


      if goal_input is not None:
        h_conv1a = tf.nn.relu(self._conv2d(state_input, W_conv1, 4) + b_conv1) # stride=4 => 19x19x16
        h_conv1b = tf.nn.relu(self._conv2d(goal_input, W_conv1, 4) + b_conv1) # stride=4 => 19x19x16
        h_conv1 = tf.concat((h_conv1a, h_conv1b,), 3)
        h_conv2 = tf.nn.relu(self._conv2d(h_conv1,     W_conv2, 2) + b_conv2) # stride=2 => 9x9x32
        pass
      else:
        h_conv1 = tf.nn.relu(self._conv2d(state_input, W_conv1, 4) + b_conv1) # stride=4 => 19x19x16
        h_conv2 = tf.nn.relu(self._conv2d(h_conv1,     W_conv2, 2) + b_conv2) # stride=2 => 9x9x32
        pass
      return h_conv2


  def _base_fcn_layer(self, conv_output, last_action_reward_objective_input,
                      reuse=False):
    with tf.variable_scope("base_fcn", reuse=reuse) as scope:
      # Weights (9x9x32=2592)
      W_fc1, b_fc1 = self._fc_variable([2592, 256], "base_fc1")

      # Nodes
      conv_output_flat = tf.reshape(conv_output, [-1, 2592])
      # (-1,9,9,32) -> (-1,2592)
      conv_output_fc = tf.nn.relu(tf.matmul(conv_output_flat, W_fc1) + b_fc1)
      # (unroll_step, 256)

      outputs = tf.concat([conv_output_fc, last_action_reward_objective_input], 1)
      return conv_output_fc


  def _base_lstm_layer(self, conv_output, last_action_reward_objective_input, initial_state_input,
                       reuse=False):
    with tf.variable_scope("base_lstm", reuse=reuse) as scope:
      # Weights (9x9x32=2592)
      W_fc1, b_fc1 = self._fc_variable([2592, 256], "base_fc1")

      # Nodes
      conv_output_flat = tf.reshape(conv_output, [-1, 2592])
      # (-1,9,9,32) -> (-1,2592)
      conv_output_fc = tf.nn.relu(tf.matmul(conv_output_flat, W_fc1) + b_fc1)
      # (unroll_step, 256)

      step_size = tf.shape(conv_output_fc)[:1]

      lstm_input = tf.concat([conv_output_fc, last_action_reward_objective_input], 1)

      # (unroll_step, 256+action_size+1+objective_size)

      lstm_input_reshaped = tf.reshape(lstm_input, [1, -1, 256+self._action_size+1])
      # (1, unroll_step, 256+action_size+1+objective_size)

      lstm_outputs, lstm_state = tf.nn.dynamic_rnn(self.lstm_cell,
                                                   lstm_input_reshaped,
                                                   initial_state = initial_state_input,
                                                   sequence_length = step_size,
                                                   time_major = False,
                                                   scope = scope)
      
      lstm_outputs = tf.reshape(lstm_outputs, [-1,256])
      #(1,unroll_step,256) for back prop, (1,1,256) for forward prop.
      return lstm_outputs, lstm_state


  def _base_policy_layer(self, lstm_outputs, reuse=False):
    with tf.variable_scope("base_policy", reuse=reuse) as scope:
      input_size = lstm_outputs.get_shape().as_list()[1]
      # Weight for policy output layer
      W_fc_p, b_fc_p = self._fc_variable([input_size, self._action_size], "base_fc_p")
      # Policy (output)
      base_pi_without_softmax = tf.matmul(lstm_outputs, W_fc_p) + b_fc_p
      base_pi = tf.nn.softmax(base_pi_without_softmax)
      return (base_pi_without_softmax, base_pi)


  def _base_value_layer(self, lstm_outputs, reuse=False):
    with tf.variable_scope("base_value", reuse=reuse) as scope:
      input_size = lstm_outputs.get_shape().as_list()[1]
      # Weight for value output layer
      W_fc_v, b_fc_v = self._fc_variable([input_size, 1], "base_fc_v")
      
      # Value (output)
      v_ = tf.matmul(lstm_outputs, W_fc_v) + b_fc_v
      base_v = tf.reshape( v_, [-1] )
      return base_v


  def _create_pc_network(self):
    # State (Image input) 
    self.pc_input = tf.placeholder("float", [None, self._image_shape[0], self._image_shape[1], 3])

    # Last action and reward and objective
    self.pc_last_action_reward_input = tf.placeholder("float", [None, self._action_size+1+self._objective_size])

    # pc conv layers
    pc_conv_output = self._base_conv_layers(self.pc_input, reuse=True)

    if self._use_lstm:
      # pc lstm layers
      pc_initial_lstm_state = self.lstm_cell.zero_state(1, tf.float32)
      # (Initial state is always reset.)

      pc_lstm_outputs, _ = self._base_lstm_layer(pc_conv_output,
                                                 self.pc_last_action_reward_input,
                                                 pc_initial_lstm_state,
                                                 reuse=True)

      self.pc_q, self.pc_q_max = self._pc_deconv_layers(pc_lstm_outputs)
    else:
      pc_fcn_outputs = self._base_fcn_layer(pc_conv_output, self.pc_last_action_reward_input, reuse=True)
      self.pc_q, self.pc_q_max = self._pc_deconv_layers(pc_fcn_outputs)

    
  def _create_pc_network_for_display(self):
    self.pc_q_disp, self.pc_q_max_disp = self._pc_deconv_layers(self.base_lstm_outputs, reuse=True)
    
  
  def _pc_deconv_layers(self, lstm_outputs, reuse=False):
    with tf.variable_scope("pc_deconv", reuse=reuse) as scope:
      input_size = lstm_outputs.get_shape().as_list()[1]
      # (Spatial map was written as 7x7x32, but here 9x9x32 is used to get 20x20 deconv result?)
      # State (image input for pixel change)
      W_pc_fc1, b_pc_fc1 = self._fc_variable([input_size, 9*9*32], "pc_fc1")
        
      W_pc_deconv_v, b_pc_deconv_v = self._conv_variable([4, 4, 1, 32],
                                                         "pc_deconv_v", deconv=True)
      W_pc_deconv_a, b_pc_deconv_a = self._conv_variable([4, 4, self._action_size, 32],
                                                         "pc_deconv_a", deconv=True)
      
      h_pc_fc1 = tf.nn.relu(tf.matmul(lstm_outputs, W_pc_fc1) + b_pc_fc1)
      h_pc_fc1_reshaped = tf.reshape(h_pc_fc1, [-1,9,9,32])
      # Dueling network for V and Advantage
      h_pc_deconv_v = tf.nn.relu(self._deconv2d(h_pc_fc1_reshaped,
                                                W_pc_deconv_v, 9, 9, 2) +
                                 b_pc_deconv_v)
      h_pc_deconv_a = tf.nn.relu(self._deconv2d(h_pc_fc1_reshaped,
                                                W_pc_deconv_a, 9, 9, 2) +
                                 b_pc_deconv_a)
      # Advantage mean
      h_pc_deconv_a_mean = tf.reduce_mean(h_pc_deconv_a, reduction_indices=3, keepdims=True)

      # {Pixel change Q (output)
      pc_q = h_pc_deconv_v + h_pc_deconv_a - h_pc_deconv_a_mean
      #(-1, 20, 20, action_size)

      # Max Q
      pc_q_max = tf.reduce_max(pc_q, reduction_indices=3, keepdims=False)
      #(-1, 20, 20)

      return pc_q, pc_q_max
    

  def _create_vr_network(self):
    # State (Image input)
    self.vr_input = tf.placeholder("float", [None, self._image_shape[0], self._image_shape[1], 3])

    # Last action and reward and objective
    self.vr_last_action_reward_input = tf.placeholder("float", [None, self._action_size+1+self._objective_size])

    # VR conv layers
    vr_conv_output = self._base_conv_layers(self.vr_input, reuse=True)

    if self._use_lstm:
      # pc lstm layers
      vr_initial_lstm_state = self.lstm_cell.zero_state(1, tf.float32)
      # (Initial state is always reset.)

      vr_lstm_outputs, _ = self._base_lstm_layer(vr_conv_output,
                                                 self.vr_last_action_reward_input,
                                                 vr_initial_lstm_state,
                                                 reuse=True)
      # value output
      self.vr_v  = self._base_value_layer(vr_lstm_outputs, reuse=True)
    else:
      vr_fcn_outputs = self._base_fcn_layer(vr_conv_output, self.vr_last_action_reward_input, reuse=True)
      self.vr_v = self._base_value_layer(vr_fcn_outputs, reuse=True)

    
  def _create_rp_network(self):
    self.rp_input = tf.placeholder("float", [3, self._image_shape[0], self._image_shape[1], 3])

    # RP conv layers
    rp_conv_output = self._base_conv_layers(self.rp_input, reuse=True)
    rp_conv_output_reshaped = tf.reshape(rp_conv_output, [1,9*9*32*3])
    
    with tf.variable_scope("rp_fc") as scope:
      # Weights
      W_fc1, b_fc1 = self._fc_variable([9*9*32*3, 3], "rp_fc1")

    # Reawrd prediction class output. (zero, positive, negative)
    self.rp_c = tf.nn.softmax(tf.matmul(rp_conv_output_reshaped, W_fc1) + b_fc1)
    # (1,3)

  def _base_loss(self):
    # [base A3C]
    # Taken action (input for policy)
    self.base_a = tf.placeholder("float", [None, self._action_size], name='base_a')
    
    # Advantage (R-V) (input for policy)
    self.base_adv = tf.placeholder("float", [None], name='base_adv')
    
    # Avoid NaN with clipping when value in pi becomes zero
    log_pi = tf.log(tf.clip_by_value(self.base_pi, 1e-20, 1.0))
    
    # Policy entropy
    entropy = -tf.reduce_sum(self.base_pi * log_pi, reduction_indices=1)
    
    # Policy loss (output)
    policy_loss = -tf.reduce_sum( tf.reduce_sum( tf.multiply( log_pi, self.base_a ),
                                                 reduction_indices=1 ) *
                                  self.base_adv + entropy * self._entropy_beta)
    
    # R (input for value target)
    self.base_r = tf.placeholder("float", [None], name='base_r')
    
    # Value loss (output)
    # (Learning rate for Critic is half of Actor's, so multiply by 0.5)
    value_loss = 0.5 * tf.nn.l2_loss(self.base_r - self.base_v)
    
    base_loss = policy_loss + value_loss
    return base_loss

  
  def _pc_loss(self):
    # [pixel change]
    self.pc_a = tf.placeholder("float", [None, self._action_size], name='pc_a')
    pc_a_reshaped = tf.reshape(self.pc_a, [-1, 1, 1, self._action_size])

    # Extract Q for taken action
    pc_qa_ = tf.multiply(self.pc_q, pc_a_reshaped)
    pc_qa = tf.reduce_sum(pc_qa_, reduction_indices=3, keepdims=False)
    # (-1, 20, 20)
      
    # TD target for Q
    self.pc_r = tf.placeholder("float", [None, 20, 20], name='pc_r')

    pc_loss = self._pixel_change_lambda * tf.nn.l2_loss(self.pc_r - pc_qa)
    return pc_loss

  
  def _vr_loss(self):
    # R (input for value)
    self.vr_r = tf.placeholder("float", [None], name='vr_r')
    
    # Value loss (output)
    vr_loss = tf.nn.l2_loss(self.vr_r - self.vr_v)
    return vr_loss


  def _rp_loss(self):
    # reward prediction target. one hot vector
    self.rp_c_target = tf.placeholder("float", [1,3], name='rp_c_target')
    
    # Reward prediction loss (output)
    rp_c = tf.clip_by_value(self.rp_c, 1e-20, 1.0)
    rp_loss = -tf.reduce_sum(self.rp_c_target * tf.log(rp_c))
    return rp_loss
    
    
  def _deepq(self):
    adventage = self.base_pi_without_softmax
    value = tf.reshape(self.base_v, [-1, 1])
    self.q_out = value + tf.subtract(adventage, tf.reduce_mean(adventage, axis=1, keep_dims=True))
    self.predict = tf.argmax(self.q_out, 1)

  def _deepq_loss(self):
    self.target_q = tf.placeholder(shape=[None],dtype=tf.float32)
    self.actions = tf.placeholder(shape=[None],dtype=tf.int32)
    self.actions_onehot = tf.one_hot(self.actions, self._action_size,dtype=tf.float32)
    self.q = tf.reduce_sum(tf.multiply(self.q_out, self.actions_onehot), axis=1)
    self.td_error = tf.square(self.target_q - self.q)
    self.total_loss = tf.reduce_mean(self.td_error)

  def prepare_loss(self):
    with tf.device(self._device):
      if self._use_deepq_network:
        self._deepq_loss()
      else:
        loss = self._base_loss()
        
        if self._use_pixel_change:
          pc_loss = self._pc_loss()
          loss = loss + pc_loss

        if self._use_value_replay:
          vr_loss = self._vr_loss()
          loss = loss + vr_loss

        if self._use_reward_prediction:
          rp_loss = self._rp_loss()
          loss = loss + rp_loss
        
        self.total_loss = loss


  def reset_state(self):
    if self._use_lstm:
      self.base_lstm_state_out = tf.contrib.rnn.LSTMStateTuple(np.zeros([1, 256]),
                                                               np.zeros([1, 256]))

  def run_base_policy_and_value(self, sess, s_t, last_action_reward):
    # This run_base_policy_and_value() is used when forward propagating.
    # so the step size is 1.
    if self._use_lstm:
      feed_dict = {self.base_input : [s_t['image']],
        self.base_last_action_reward_input : [last_action_reward],
        self.base_initial_lstm_state0 : self.base_lstm_state_out[0],
        self.base_initial_lstm_state1 : self.base_lstm_state_out[1]}

      if self._use_goal_input:
        feed_dict[self.goal_input] = [s_t['goal']]

      pi_out, v_out, self.base_lstm_state_out = sess.run( [self.base_pi, self.base_v, self.base_lstm_state],
                                                          feed_dict = feed_dict )
    else:
      feed_dict = {self.base_input : [s_t['image']],
        self.base_last_action_reward_input : [last_action_reward]}

      if self._use_goal_input:
        feed_dict[self.goal_input] = [s_t['goal']]

      pi_out, v_out = sess.run([self.base_pi, self.base_v],
                               feed_dict = feed_dict)

    # pi_out: (1,3), v_out: (1)
    return (pi_out[0], v_out[0])

  
  def run_base_policy_value_pc_q(self, sess, s_t, last_action_reward):
    # For display tool.
    if self._use_lstm:
      feed_dict = {self.base_input : [s_t['image']],
        self.base_last_action_reward_input : [last_action_reward],
        self.base_initial_lstm_state0 : self.base_lstm_state_out[0],
        self.base_initial_lstm_state1 : self.base_lstm_state_out[1]}

      if self._use_goal_input:
        feed_dict[self.goal_input] = [s_t['goal']]
        
      pi_out, v_out, self.base_lstm_state_out, q_disp_out, q_max_disp_out = \
          sess.run( [self.base_pi, self.base_v, self.base_lstm_state, self.pc_q_disp, self.pc_q_max_disp],
                    feed_dict = feed_dict)
    else:
      feed_dict = {self.base_input : [s_t['image']],
        self.base_last_action_reward_input : [last_action_reward] }

      if self._use_goal_input:
        feed_dict[self.goal_input] = [s_t['goal']]

      pi_out, v_out, q_disp_out, q_max_disp_out = \
        sess.run( [self.base_pi, self.base_v, self.pc_q_disp, self.pc_q_max_disp],
                  feed_dict = feed_dict)

    # pi_out: (1,3), v_out: (1), q_disp_out(1,20,20, action_size)
    return (pi_out[0], v_out[0], q_disp_out[0])

  
  def run_base_value(self, sess, s_t, last_action_reward):
    # This run_base_value() is used for calculating V for bootstrapping at the
    # end of LOCAL_T_MAX time step sequence.
    # When next sequence starts, V will be calculated again with the same state using updated network weights,
    # so we don't update LSTM state here.
    if self._use_lstm:
      feed_dict = {self.base_input : [s_t['image']],
        self.base_last_action_reward_input : [last_action_reward],
        self.base_initial_lstm_state0 : self.base_lstm_state_out[0],
        self.base_initial_lstm_state1 : self.base_lstm_state_out[1]}
      if self._use_goal_input:
        feed_dict[self.goal_input] = [s_t['goal']]
      
      v_out, _ = sess.run( [self.base_v, self.base_lstm_state],
                           feed_dict = feed_dict)
    else:
      feed_dict = {self.base_input : [s_t['image']],
        self.base_last_action_reward_input : [last_action_reward]}
      if self._use_goal_input:
        feed_dict[self.goal_input] = [s_t['goal']]

      v_out = sess.run( self.base_v,
                        feed_dict = feed_dict)
    return v_out[0]

  
  def run_pc_q_max(self, sess, s_t, last_action_reward):
    q_max_out = sess.run( self.pc_q_max,
                          feed_dict = {self.pc_input : [s_t['image']],
                                       self.pc_last_action_reward_input : [last_action_reward]} )
    return q_max_out[0]

  
  def run_vr_value(self, sess, s_t, last_action_reward):
    vr_v_out = sess.run( self.vr_v,
                         feed_dict = {self.vr_input : [s_t['image']],
                                      self.vr_last_action_reward_input : [last_action_reward]} )
    return vr_v_out[0]

  
  def run_rp_c(self, sess, state_history):
    # For display tool
    frames = [s_t['image'] for s_t in state_history]
    rp_c_out = sess.run( self.rp_c,
                         feed_dict = {self.rp_input : frames} )
    return rp_c_out[0]

  
  def get_vars(self):
    return self.variables
  

  def sync_from(self, src_network, name=None):
    src_vars = src_network.get_vars()
    dst_vars = self.get_vars()

    sync_ops = []

    with tf.device(self._device):
      with tf.name_scope(name, "UnrealModel",[]) as name:
        for(src_var, dst_var) in zip(src_vars, dst_vars):
          sync_op = tf.assign(dst_var, src_var)
          sync_ops.append(sync_op)

        return tf.group(*sync_ops, name=name)
      

  def _fc_variable(self, weight_shape, name):
    name_w = "W_{0}".format(name)
    name_b = "b_{0}".format(name)
    
    input_channels  = weight_shape[0]
    output_channels = weight_shape[1]
    bias_shape = [output_channels]

    weight = tf.get_variable(name_w, weight_shape, initializer=fc_initializer(input_channels))
    bias   = tf.get_variable(name_b, bias_shape,   initializer=fc_initializer(input_channels))
    return weight, bias

  
  def _conv_variable(self, weight_shape, name, deconv=False):
    name_w = "W_{0}".format(name)
    name_b = "b_{0}".format(name)
    
    w = weight_shape[0]
    h = weight_shape[1]
    if deconv:
      input_channels  = weight_shape[3]
      output_channels = weight_shape[2]
    else:
      input_channels  = weight_shape[2]
      output_channels = weight_shape[3]
    bias_shape = [output_channels]

    weight = tf.get_variable(name_w, weight_shape,
                             initializer=conv_initializer(w, h, input_channels))
    bias   = tf.get_variable(name_b, bias_shape,
                             initializer=conv_initializer(w, h, input_channels))
    return weight, bias

  
  def _conv2d(self, x, W, stride):
    return tf.nn.conv2d(x, W, strides = [1, stride, stride, 1], padding = "VALID")


  def _get2d_deconv_output_size(self,
                                input_height, input_width,
                                filter_height, filter_width,
                                stride, padding_type):
    if padding_type == 'VALID':
      out_height = (input_height - 1) * stride + filter_height
      out_width  = (input_width  - 1) * stride + filter_width
      
    elif padding_type == 'SAME':
      out_height = input_height * stride
      out_width  = input_width  * stride
    
    return out_height, out_width


  def _deconv2d(self, x, W, input_width, input_height, stride):
    filter_height = W.get_shape()[0].value
    filter_width  = W.get_shape()[1].value
    out_channel   = W.get_shape()[2].value
    
    out_height, out_width = self._get2d_deconv_output_size(input_height,
                                                           input_width,
                                                           filter_height,
                                                           filter_width,
                                                           stride,
                                                           'VALID')
    batch_size = tf.shape(x)[0]
    output_shape = tf.stack([batch_size, out_height, out_width, out_channel])
    return tf.nn.conv2d_transpose(x, W, output_shape,
                                  strides=[1, stride, stride, 1],
                                  padding='VALID')
#!/usr/bin/env python3
# Usage:
#  PYTHONPATH=src ./train --dataset <file|directory|glob>
import os
import sys
sys.path += [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')]

import argparse
import json
import numpy as np
import tensorflow as tf
import time
import tqdm
from tensorflow.core.protobuf import rewriter_config_pb2
from tensorflow.python import pywrap_tensorflow
from tensorflow.python.ops import gradients
from tensorflow.core.protobuf import config_pb2

import model, sample, encoder
from load_dataset import load_dataset, Sampler, TextSampler, TokenSampler
from accumulate import AccumulatingOptimizer
import memory_saving_gradients
from glob import glob
import re
import tflex
import tflex_sgdr

import pytz
from datetime import datetime, timezone

import threading

CHECKPOINT_DIR = 'checkpoint'
SAMPLE_DIR = 'samples'

tflex.child_pid = 0

parser = argparse.ArgumentParser(
    description='Fine-tune GPT-2 on your custom dataset.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--dataset', metavar='PATH', type=str, required=True, help='Input file, directory, or glob pattern (utf-8 text, or preencoded .npz files).')
parser.add_argument('--model_name', metavar='MODEL', type=str, default='117M', help='Pretrained model name')
parser.add_argument('--combine', metavar='CHARS', type=int, default=50000, help='Concatenate input files with <|endoftext|> separator into chunks of this minimum size')

parser.add_argument('--batch_size', metavar='SIZE', type=int, default=1, help='Batch size')
parser.add_argument('--learning_rate', metavar='LR', type=float, default=0.00002, help='Learning rate for Adam')
parser.add_argument('--learning_rate_min', type=float, default=0.00001, help='Minimum learning rate')
parser.add_argument('--learning_rate_cos', default=False, action='store_true', help='Use learn rate cosine annealing')
parser.add_argument('--learning_rate_warmup', type=int, default=100, help='Learning rate warmup for cosine annealing')
parser.add_argument('--learning_rate_period', type=int, default=100, help='Learning rate period for cosine annealing')
parser.add_argument('--learning_rate_initial_step', type=int, default=0, help='Learning rate initial step for cosine annealing')
parser.add_argument('--accumulate_gradients', metavar='N', type=int, default=1, help='Accumulate gradients across N minibatches.')
parser.add_argument('--memory_saving_gradients', default=False, action='store_true', help='Use gradient checkpointing to reduce vram usage.')
parser.add_argument('--only_train_transformer_layers', default=False, action='store_true', help='Restrict training to the transformer blocks.')
parser.add_argument('--optimizer', type=str, default='adam', help='Optimizer. <adam|sgd|ada>.')
parser.add_argument('--noise', type=float, default=0.0, help='Add noise to input training data to regularize against typos.')

parser.add_argument('--top_k', type=int, default=40, help='K for top-k sampling.')
parser.add_argument('--top_p', type=float, default=0.0, help='P for top-p sampling. Overrides top_k if set > 0.')

parser.add_argument('--restore_from', type=str, default='latest', help='Either "latest", "fresh", or a path to a checkpoint file')
parser.add_argument('--run_name', type=str, default='run1', help='Run id. Name of subdirectory in checkpoint/ and samples/')
parser.add_argument('--sample_every', metavar='N', type=int, default=100, help='Generate samples every N steps')
parser.add_argument('--sample_length', metavar='TOKENS', type=int, default=-1, help='Sample this many tokens')
parser.add_argument('--sample_num', metavar='N', type=int, default=-1, help='Generate this many samples')
parser.add_argument('--save_every', metavar='N', type=int, default=-1, help='Write a checkpoint every N steps')
parser.add_argument('--save_time', metavar='N', type=float, default=15.0, help='Write a checkpoint every N minutes')
parser.add_argument('--max_to_keep', metavar='N', type=int, default=5, help='Only keep the last N checkpoints')

parser.add_argument('--val_dataset', metavar='PATH', type=str, default=None, help='Dataset for validation loss, defaults to --dataset.')
parser.add_argument('--val_batch_size', metavar='SIZE', type=int, default=-1, help='Batch size for validation.')
parser.add_argument('--val_batch_count', metavar='N', type=int, default=-1, help='Number of batches for validation.')
parser.add_argument('--val_every', metavar='STEPS', type=int, default=0, help='Calculate validation loss every STEPS steps.')

parser.add_argument('--sample_batch_size', metavar='SIZE', type=int, default=-1, help='Batch size for validation.')

parser.add_argument('--init_tpu', default=False, action='store_true', help='Initialize TPU session.')

parser.add_argument('--fresh_model', default=False, action='store_true', help="Don't load model from disk; initialize model weights to random values")
parser.add_argument('--save_on_ctrlc', default=False, action='store_true', help='When execution is interrupted, should we save the model to disk?')
parser.add_argument('--debug_on_ctrlc', default=False, action='store_true', help='When execution is interrupted, attach a debugger (pdb.set_trace())')
parser.add_argument('--dtype', type=str, default='float32', help='dtype. <float32|float16|bfloat16>.')

parser.add_argument('--fork', type=str, default='')

# 1.5B
#parser.add_argument('--n_ctx', type=int, default=1024, help='For a fresh model, how large should n_ctx be?')
#parser.add_argument('--n_embd', type=int, default=1600, help='For a fresh model, how large should n_embd be?')
#parser.add_argument('--n_head', type=int, default=25, help='For a fresh model, how large should n_head be?')
#parser.add_argument('--n_layer', type=int, default=48, help='For a fresh model, how large should n_layer be?')

# 345M
#parser.add_argument('--n_ctx', type=int, default=1024, help='For a fresh model, how large should n_ctx be?')
#parser.add_argument('--n_embd', type=int, default=1024, help='For a fresh model, how large should n_embd be?')
#parser.add_argument('--n_head', type=int, default=16, help='For a fresh model, how large should n_head be?')
#parser.add_argument('--n_layer', type=int, default=24, help='For a fresh model, how large should n_layer be?')

# 117M
#parser.add_argument('--n_ctx', type=int, default=1024, help='For a fresh model, how large should n_ctx be?')
#parser.add_argument('--n_embd', type=int, default=768, help='For a fresh model, how large should n_embd be?')
#parser.add_argument('--n_head', type=int, default=12, help='For a fresh model, how large should n_head be?')
#parser.add_argument('--n_layer', type=int, default=12, help='For a fresh model, how large should n_layer be?')

parser.add_argument('--n_ctx', type=int, default=-1, help='For a fresh model, how large should n_ctx be?')
parser.add_argument('--n_embd', type=int, default=-1, help='For a fresh model, how large should n_embd be?')
parser.add_argument('--n_head', type=int, default=-1, help='For a fresh model, how large should n_head be?')
parser.add_argument('--n_layer', type=int, default=-1, help='For a fresh model, how large should n_layer be?')

parser.add_argument('--sample_ctx', type=int, default=-1, help='Compute loss over N samples. Equal to n_ctx if set < 0.')

parser.add_argument('--truncate_weights', default=False, action='store_true', help="Try loading variables from snapshots, even if those variables' shapes do not match")

parser.add_argument('--debug_print_all_vars', default=False, action='store_true', help="Print all variables after running one training step")
parser.add_argument('--debug_print_trainable_vars', default=False, action='store_true', help="Print trainable variables after running one training step")

parser.add_argument('--allow_growth', default=False, action='store_true', help="Set config.gpu_options.allow_growth = True")
parser.add_argument('--allow_soft_placement', default=False, action='store_true', help="Set config.allow_soft_placement = True")
parser.add_argument('--disable_layout_optimizer', default=False, action='store_true', help="Set config.graph_options.rewrite_options.layout_optimizer = rewriter_config_pb2.RewriterConfig.OFF")
parser.add_argument('--colocate_gradients', default=False, action='store_true')
parser.add_argument('--no_report_tensor_allocations_upon_oom', default=True, action='store_false')

parser.add_argument('--debug_before_training', default=False, action='store_true', help="Drop into debugger before starting the training loop")

parser.add_argument('--dropout', type=float, default=0.0, help="Dropout value. Disabled if set <= 0.0. For training on large datasets, 0.1 tends to be a good value.")

parser.add_argument('--seed', type=int, default=-1, help='Deterministic seed for dataset sampler. Disabled if set < 0')

parser.add_argument('--save_graph', default=False, action='store_true', help="Save TensorFlow graph to summary log (to see ops in tensorboard)")
parser.add_argument('--max_cores', type=int, default=-1)
parser.add_argument('--skip_cores', type=int, default=0)

parser.add_argument('--device', type=int, default=-1, help='device to use.')

PST = pytz.timezone('US/Pacific')

def timestamp(now=None, tz=None):
    if now is None:
        now = datetime.now(timezone.utc)
    if tz is None:
        tz = PST
    return "{}".format(now.astimezone(tz).isoformat())

def maketree(path):
    try:
        os.makedirs(path)
    except:
        pass


def randomize(context, hparams, p):
    if p > 0:
        mask = tf.random.uniform(shape=tf.shape(context)) < p
        noise = tf.random.uniform(shape=tf.shape(context), minval=0, maxval=hparams.n_vocab, dtype=tf.int32)
        return tf.where(mask, noise, context)
    else:
        return context


def main():
    args = parser.parse_args()
    enc = encoder.get_encoder(args.model_name)
    hparams = model.default_hparams()
    hparams.res_dropout = args.dropout
    hparams.attn_dropout = args.dropout
    epsilon = -1e10
    if args.dtype == 'float32':
        hparams.dtype = tf.float32
    elif args.dtype == 'float16':
        hparams.dtype = tf.float16
        epsilon = -65500
    elif args.dtype == 'bfloat16':
        hparams.dtype = tf.bfloat16
    else:
        print('Unknown dtype', args.dtype)

    with open(os.path.join('models', args.model_name, 'hparams.json')) as f:
        hparams.override_from_dict(json.load(f))
    if args.n_ctx >= 0:
        hparams.n_ctx=args.n_ctx
    if args.n_embd >= 0:
        hparams.n_embd=args.n_embd
    if args.n_head >= 0:
        hparams.n_head=args.n_head
    if args.n_layer >= 0:
        hparams.n_layer=args.n_layer

    if args.sample_num < 0:
        args.sample_num = 1
    if args.sample_batch_size < 0:
        args.sample_batch_size = 1

    if args.val_batch_size < 0:
        args.val_batch_size = 1
    if args.val_batch_count < 0:
        args.val_batch_count = 80 // args.val_batch_size

    if args.sample_length < 0:
        args.sample_length = min(64, hparams.n_ctx) - 1
    if args.sample_length > hparams.n_ctx:
        raise ValueError(
            "Can't get samples longer than window size: %s" % hparams.n_ctx)
    if args.sample_ctx < 0:
      args.sample_ctx = hparams.n_ctx

    if args.model_name == '345M':
        args.memory_saving_gradients = True
        if args.optimizer == 'adam':
            args.only_train_transformer_layers = True

    config = tf.ConfigProto()
    config.allow_soft_placement = False
    if args.allow_growth:
        config.gpu_options.allow_growth = True
    if args.allow_soft_placement:
        config.allow_soft_placement = True
    if args.disable_layout_optimizer:
        config.graph_options.rewrite_options.layout_optimizer = rewriter_config_pb2.RewriterConfig.OFF

    #def build_graph(graph=None):
    #  if graph is None:
    #    graph = tf.Graph()
    #  with graph.as_default():

    tflex.run_options = config_pb2.RunOptions(report_tensor_allocations_upon_oom=(not args.no_report_tensor_allocations_upon_oom))
    tflex.graph = tf.Graph()
    with tflex.graph.as_default():
      #tflex.init_tpu_op = tf.contrib.tpu.initialize_system()
      tflex.init_tpu_op = None
    def initialize_tpu(session=None, timeout_in_ms=15000):
      if session is None:
        session = tflex.sess
      options = None
      if timeout_in_ms:
        options=config_pb2.RunOptions(timeout_in_ms=timeout_in_ms)
      print('Initializing TPU...')
      init_tpu_op = tflex.init_tpu_op
      if init_tpu_op is not None:
        session.run(init_tpu_op, options=options)
        print('Initialized TPU')
        return True
      return False
    tflex.sess = tflex.Session(config=config, graph=tflex.graph)
    if args.init_tpu:
      initialize_tpu()
    with tflex.graph.as_default():
        #tflex.sess.ensure()
        with tf.variable_scope(tf.get_variable_scope().name, reuse=tf.AUTO_REUSE):
            global_step = tflex.get_variable('global_step') or tf.get_variable('global_step', shape=(), dtype=tf.int32, trainable=False)
            current_step = args.learning_rate_initial_step
            #global_step.load(current_step, session=tflex.sess)
            if args.learning_rate_cos:
                lr = tflex_sgdr.sgdr_decay_with_warmup(args.learning_rate, global_step,
                    warmup_steps=args.learning_rate_warmup, initial_period_steps=args.learning_rate_period, learning_rate_min=args.learning_rate_min)
            else:
                lr = tflex.get_variable('learn_rate') or tf.get_variable('learn_rate', shape=(), dtype=tf.float32, trainable=False)
                #lr.load(args.learning_rate, session=tflex.sess)
    devices = tflex.sess.list_devices()
    device = None if args.device < 0 else devices[args.device].name
    with tflex.graph.as_default(), tf.device(device):
        output = model.shard(batch_size=args.batch_size, hparams=hparams, noise=args.noise, learning_rate=lr, optimizer=args.optimizer, only_train_transformer_layers=args.only_train_transformer_layers, colocate_gradients_with_ops=args.colocate_gradients, use_memory_saving_gradients=args.memory_saving_gradients, max_cores=args.max_cores, skip_cores=args.skip_cores, devices=devices)
        shards = output['shards']
        feed = output['feed']
        the = output['the']
        opt_loss = the.opt_loss
        opt_apply = the.opt_apply
        opt_gather = the.opt_gather
        opt_train = the.opt_train

        def trainer_reset_variables(variables):
          ops = [the.reset_var[v.name] for v in variables]
          tflex.sess.run(ops)

        tflex.trainer_reset_variables = trainer_reset_variables

        #val_context = tf.placeholder(tf.int32, [args.val_batch_size, None])
        val_context = tf.Variable(tf.zeros(shape=[args.val_batch_size, args.sample_ctx], dtype=tf.int32), dtype=tf.int32, name="val_context")
        val_output = model.model(hparams=hparams, X=val_context)
        val_loss = tf.reduce_mean(
            tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=val_context[:, 1:], logits=val_output['logits'][:, :-1]))
        val_loss_summary = tf.summary.scalar('val_loss', val_loss)


        tflex.tf_sample = None
        tflex.sample_context = None

        #all_vars = [v for v in tf.trainable_variables() if 'model' in v.name]
        #train_vars = [v for v in all_vars if '/h' in v.name or '/ln_f' in v.name] if args.only_train_transformer_layers else all_vars

        #parameter_count = sum([np.prod(v.shape.as_list()) for v in train_vars])
        #print("This model is using %d parameters (%.2fM)" % (parameter_count, parameter_count/(1024.0*1024.0)))

        def update_lr(rate=None, step=None):
          if not args.learning_rate_cos:
            if step is None:
              step = global_step.eval(session=tflex.sess)
            if rate is None:
              rate = args.learning_rate
            if callable(rate):
              rate = rate(step)
            lr.load(rate, session=tflex.sess)
          return lr.eval(session=tflex.sess)

        @tflex.register_command
        def set_learning_rate():
          print("Current learn rate: %0.8f" % update_lr())
          print("New learn rate?")
          rate = input('')
          if not rate:
            print("Empty input; not changing anything.")
          else:
            try:
              rate = float(rate)
            except:
              print("Invalid input; must be a float")
          print("Setting learn rate to %0.8f" % rate)
          args.learning_rate = rate

        #if args.optimizer == 'adam':
        #    opt = tf.train.AdamOptimizer(learning_rate=lr)
        #elif args.optimizer == 'sgd':
        #    opt = tf.train.GradientDescentOptimizer(learning_rate=lr)
        #elif args.optimizer == 'ada':
        #    import tensor2tensor.utils.optimize
        #    from tensor2tensor.utils import hparam
        #    import tensor2tensor.models.research
        #    from tensor2tensor.utils import registry
        #    ada_hparams = registry.hparams('afx_mimic_adam')
        #    ada_hparams.optimizer_adafactor_beta1 = 0.0
        #    ada_hparams.optimizer_adafactor_factored = True
        #    opt = tensor2tensor.utils.optimize.adafactor(learning_rate=lr, hparams=ada_hparams)
        #else:
        #    exit('Bad optimizer:', args.optimizer)
        #
        ##if tpu_addr:
        ##    # https://pulsejet.github.io/blog/posts/tpu-without-estimator/
        ##    from tensorflow.contrib.tpu.python.tpu import tpu_function
        ##    tpu_function.get_tpu_context().set_number_of_shards(8)
        ##    opt = tf.contrib.tpu.CrossShardOptimizer(opt)

        #if args.accumulate_gradients > 1:
        #    if args.memory_saving_gradients:
        #        exit("Memory saving gradients are not implemented for gradient accumulation yet.")
        #    opt = AccumulatingOptimizer(
        #        opt=opt,
        #        var_list=train_vars)
        #    opt_reset = opt.reset()
        #    opt_compute = opt.compute_gradients(loss)
        #    opt_apply = opt.apply_gradients()
        #    summary_loss = tf.summary.scalar('loss', opt_apply)
        #else:
        #    if args.memory_saving_gradients:
        #        opt_grads = memory_saving_gradients.gradients(loss, train_vars, colocate_gradients_with_ops=args.colocate_gradients)
        #    else:
        #        opt_grads = gradients.gradients(loss, train_vars, colocate_gradients_with_device=args.colocate_gradients)
        #    opt_grads = list(zip(opt_grads, train_vars))
        #    opt_apply = opt.apply_gradients(opt_grads)
        #    summary_loss = tf.summary.scalar('loss', loss)

        summary_loss = tf.summary.scalar('loss', opt_loss)
        summary_lr = tf.summary.scalar('learning_rate', lr)
        summaries = tf.summary.merge([summary_lr, summary_loss])

        tflex.summary_log = None
        
        def reopen_log(run_name=None, target=None):
          if run_name is None:
            run_name = args.run_name
          if target is None:
            target = tflex.sess.target
          if target is not None:
            run_name = run_name + "_" + target
            run_name = run_name.replace('/', '_').replace(':', '_').replace('.', '_')
          return tf.summary.FileWriter(os.path.join(CHECKPOINT_DIR, run_name))

        tflex.summary_log = reopen_log()

        tflex.old_sessions = []
        tflex.old_logs = []
        
        def reopen(target):
          with tf.device(None):
            print(target, 'reopen: creating session')
            sess = tflex.Session(target, config=tflex.sess.config, graph=tflex.graph)
            print(target, 'reopen: ensuring TPU')
            if args.init_tpu:
              initialize_tpu(session=sess)
            print(target, 'Initializing...')
            #sess.run(tflex.init_op)
            with sess.graph.as_default():
              sess.run(tf.global_variables_initializer())
            print('Broadcasting variables...')
            tflex.trainer_reset_variables(self.all_vars)
            #with sess.as_default():
            #  sess.ensure()
            print(target, 'reopen: reopening log')
            summary_log = reopen_log(target=target)
            tflex.old_logs.append(tflex.summary_log)
            tflex.old_sessions.append(tflex.sess)
            tflex.summary_log = summary_log
            tflex.sess = sess
            reload_snapshot()
            print(target, 'reopen: done!')

        if args.save_graph:
            tflex.summary_log.add_graph(tf.get_default_graph())

        train_vars = shards[0].train_vars
        all_vars = shards[0].all_vars
        tflex.saver = tflex.Saver(
            var_list=all_vars,
            max_to_keep=args.max_to_keep,
            keep_checkpoint_every_n_hours=2,
            reshape=args.truncate_weights)
        tflex.init_op = tf.global_variables_initializer()
        tflex.sess.run(tflex.init_op)

        def reload_snapshot():
          if args.restore_from == 'latest':
              ckpt = tflex.latest_checkpoint(
                  os.path.join(CHECKPOINT_DIR, args.run_name))
              if ckpt is None:
                  # Get fresh GPT weights if new run.
                  ckpt = tflex.latest_checkpoint(
                      os.path.join('models', args.model_name))
          elif args.restore_from == 'fresh':
              ckpt = tflex.latest_checkpoint(
                  os.path.join('models', args.model_name))
          else:
              ckpt = tflex.latest_checkpoint(args.restore_from)
          print('Loading snapshot %s...' % ckpt)
          t0 = time.time()
          if not args.fresh_model:
              tflex.saver.restore(tflex.sess, ckpt)
          print('Broadcasting variables...')
          tflex.trainer_reset_variables(all_vars)
          t1 = time.time()
          print('Loaded in %f seconds' % (t1 - t0))

        reload_snapshot()

        def make_sampler(dataset, enc, seed, combine):
          if os.path.isdir(dataset) or dataset.endswith('.npz'):
            chunks = load_dataset(enc, dataset, combine)
            data_sampler = Sampler(chunks, seed=seed)
          elif dataset.endswith('.tok16'):
            data_sampler = TokenSampler(dataset, enc, seed=seed, half=True)
          elif dataset.endswith('.tok32'):
            data_sampler = TokenSampler(dataset, enc, seed=seed, half=False)
          elif dataset.endswith('.tok'):
            assert not dataset.endswith('.tok')
            #data_sampler = TokenSampler(dataset, enc, seed=seed, half=False)
          else:
            data_sampler = TextSampler(dataset, enc, seed=seed)
          return data_sampler

        print('Loading dataset...')
        seed = None if args.seed < 0 else args.seed
        data_sampler = make_sampler(dataset=args.dataset, enc=enc, seed=seed, combine=args.combine)
        # Sample from validation set once with fixed seed to make
        # it deterministic during training as well as across runs.
        val_dataset = args.val_dataset if args.val_dataset else args.dataset
        val_data_sampler = make_sampler(dataset=val_dataset, enc=enc, seed=1, combine=args.combine)
        val_batches = [[val_data_sampler.sample(hparams.n_ctx) for _ in range(args.val_batch_size)]
                       for _ in range(args.val_batch_count)]

        print('Training...')
        counter = 1
        counter_path = os.path.join(CHECKPOINT_DIR, args.run_name, 'counter')
        if os.path.exists(counter_path):
            # Load the step number if we're resuming a run
            # Add 1 so we don't immediately try to save again
            with open(counter_path, 'r') as fp:
                counter = int(fp.read()) + 1

        @tflex.register_command
        def save():
            maketree(os.path.join(CHECKPOINT_DIR, args.run_name))
            print(
                'Saving',
                os.path.join(CHECKPOINT_DIR, args.run_name,
                             'model-{}').format(counter))
            t0 = time.time()
            tflex.saver.save(
                tflex.sess,
                os.path.join(CHECKPOINT_DIR, args.run_name, 'model'),
                global_step=counter)
            t1 = time.time()
            print('Saved in %f seconds' % (t1 - t0))
            with open(counter_path, 'w') as fp:
                fp.write(str(counter) + '\n')

        @tflex.register_command
        def generate_samples():
            if tflex.tf_sample is None:
              with tf.device(None):
                tflex.sample_context = tf.Variable(tf.zeros(shape=[args.sample_batch_size, 1], dtype=tf.int32), dtype=tf.int32, name="sample_context")
                print('Initializing sampler...')
                tflex.tf_sample = sample.sample_sequence(
                    hparams=hparams,
                    length=args.sample_length,
                    context=tflex.sample_context,
                    batch_size=args.sample_batch_size,
                    temperature=1.0,
                    top_k=args.top_k,
                    top_p=args.top_p,
                    epsilon=epsilon)
            print('Generating samples...')
            context_tokens = data_sampler.sample(1)
            all_text = []
            index = 0
            tflex.sample_context.load([data_sampler.sample(1) for _ in range(args.sample_batch_size)], session=tflex.sess)
            while index < args.sample_num:
                out = tflex.sess.run(tflex.tf_sample)
                for i, tokens in enumerate(out): #range(min(args.sample_num - index, args.sample_batch_size)):
                    text = enc.decode(tokens)
                    text = '======== SAMPLE {} ========\n{}\n'.format(index + 1, text)
                    print(text)
                    all_text.append(text)
                    index += 1
            maketree(os.path.join(SAMPLE_DIR, args.run_name))
            with open(
                    os.path.join(SAMPLE_DIR, args.run_name,
                                 'samples-{}').format(counter), 'w') as fp:
                fp.write('\n'.join(all_text))

        def do_validation():
            print('Calculating validation loss...')
            losses = []
            for batch in tqdm.tqdm(val_batches):
                val_context.load(batch, session=tflex.sess)
                v_loss = tflex.sess.run(val_loss)
                losses.append(v_loss)
            v_val_loss = np.mean(losses)
            v_summary = tflex.sess.run(val_loss_summary, feed_dict={val_loss: v_val_loss})
            tflex.summary_log.add_summary(v_summary, counter)
            tflex.summary_log.flush()
            print(
                '{stamp} [{counter} | {time:2.4f}] validation loss = {loss:2.4f}'
                .format(
                    stamp=timestamp(),
                    counter=counter,
                    time=time.time() - start_time,
                    loss=v_val_loss))

        @tflex.register_command
        def validation():
          tflex.validation_thread = threading.Thread(target=do_validation)
          tflex.validation_thread.start()

        start_time = time.time()
        
        def elapsed():
            return time.time() - start_time

        def say(msg, *args):
            print('{stamp} {target} [{counter} | {time:2.4f}] {msg} {args}'.format(counter=counter, target=(tflex.sess.target or '')[-16:], time=elapsed(), msg=msg, stamp=timestamp(),
                args=('' if len(args) <= 0 else args)))

        def sample_batch():
            #return [data_sampler.sample(args.sample_ctx) for _ in range(args.batch_size)]
            #say('Sampling batch...')
            r = []
            times = []
            for _ in range(args.batch_size):
                start = time.time()
                sample = data_sampler.sample(args.sample_ctx)
                end = time.time()
                elapsed = (end - start)
                r += [sample]
                times += [elapsed]
            total = sum(times)
            avg = total / len(times)
            #say('Sampled %d batches in %.4f seconds (avg per batch: %.4f)' % (args.batch_size, total, avg))
            return r

        prev_time = time.time()
        avg_loss = (0.0, 0.0)

        if args.debug_before_training:
            import pdb
            pdb.set_trace()

        #if args.accumulate_gradients <= 1:
        #    opt_train = tf.tuple([loss, summaries], control_inputs=[opt_apply])
        tflex.run_generate_batch = True
        tflex.run_load_context = True
        tflex.run_opt_apply = True
        tflex.run_opt_gather = True
        tflex.run_summaries = False # True

        last_saved_time = elapsed()
        while True:
            try:
                now = elapsed()
                if args.save_time > 0 and (((now - last_saved_time) / 60.0) >= args.save_time):
                    save()
                    last_saved_time = now
                elif args.save_every > 0 and (counter % args.save_every == 0):
                    save()
                if counter % args.sample_every == 0:
                    generate_samples()
                if args.val_every > 0 and (counter % args.val_every == 0 or counter == 1):
                    validation()

                v_rate = update_lr()

                if args.accumulate_gradients > 1:
                    #say('Running opt_reset...')
                    tflex.sess.run(opt_reset)
                    for _ in range(args.accumulate_gradients):
                        batch = sample_batch()
                        print(repr(enc.decode(batch[0]))[0:150] + '...')
                        say('Running opt_compute...')
                        #context.load(batch, session=tflex.sess)
                        feed(batch, session=tflex.sess)
                        tflex.sess.run(opt_compute)
                    say('Running opt_apply...')
                    (v_loss, v_summary) = tflex.sess.run((opt_apply, summaries))
                else:
                    if tflex.run_generate_batch:
                      say('Generating batch...')
                      batch = sample_batch()
                      print(repr(enc.decode(batch[0]))[0:150] + '...')
                    else:
                      batch = [[0]*args.sample_ctx for _ in range(args.batch_size)]
                    if tflex.run_load_context:
                      say('Loading context...')
                      #context.load(batch, session=tflex.sess)
                      feed(batch, session=tflex.sess)
                    if tflex.run_opt_apply:
                      say('Running opt_apply...')
                      #(v_loss, v_summary) = tflex.sess.run(opt_train)
                      v_losses = tflex.sess.run(opt_train)
                      v_loss = sum(v_losses) / len(v_losses)
                    else:
                      v_losses = [11.0]
                      v_loss = 11.0
                    if tflex.run_summaries:
                      say('Running summaries...')
                      v_summary = tflex.sess.run(summaries, feed_dict={opt_loss: v_loss})
                    else:
                      v_summary = None
                    print(v_losses, v_loss)
                    if tflex.run_opt_gather:
                      say('Running opt_gather...')
                      tflex.sess.run(opt_gather, options=tflex.run_options)

                if v_summary is not None:
                  say('Adding to summary log...')
                  tflex.summary_log.add_summary(v_summary, counter)
                  tflex.summary_log.flush()

                avg_loss = (avg_loss[0] * 0.99 + v_loss,
                            avg_loss[1] * 0.99 + 1.0)

                now = time.time()
                print('{stamp} {target} [{counter} | {time:2.4f} | {delta:2.2f}s | {ops:2.6f}tokens/s] loss={loss:2.4f} avg={avg:2.4f} rate={rate:0.12f} step={step}'
                    .format(
                        stamp=timestamp(),
                        target=(tflex.sess.target or '')[-16:],
                        counter=counter,
                        time=now - start_time,
                        delta=now - prev_time,
                        ops=args.sample_ctx * args.batch_size / (now - prev_time),
                        rate=v_rate,
                        loss=v_loss,
                        avg=avg_loss[0] / avg_loss[1],
                        step=current_step,
                        ))

                counter += 1
                current_step += 1
                global_step.load(current_step, session=tflex.sess)

                if not tflex.child_pid:
                  tflex.check_commands_with_args(
                      session=tflex.sess,
                      stamp=timestamp(),
                      counter=counter,
                      time=now - start_time,
                      delta=now - prev_time,
                      ops=args.batch_size / (now - prev_time),
                      rate=v_rate,
                      loss=v_loss,
                      avg=avg_loss[0] / avg_loss[1],
                      avg_loss=avg_loss,
                      step=current_step,
                      train_vars=train_vars,
                      all_vars=all_vars,
                      args=args,
                      data_sampler=data_sampler,
                      saver=tflex.saver,
                      )
                if tflex.should_quit():
                  break

                prev_time = now
                if args.debug_print_all_vars:
                    print('all variables:')
                    print('name/shape/parameter_count')
                    param_count = 0
                    for x in tf.all_variables():
                        shape = x.shape.as_list()
                        count = np.prod(shape)
                        print(x.name, shape, count)
                        param_count += count
                    print('Total parameters:', param_count)
                    args.debug_print_all_vars = False

                if args.debug_print_trainable_vars:
                    print('trainable variables:')
                    print('name/shape/parameter_count')
                    param_count = 0
                    for x in tf.trainable_variables():
                        shape = x.shape.as_list()
                        count = np.prod(shape)
                        print(x.name, shape, count)
                        param_count += count
                    print('Total parameters:', param_count)
                    args.debug_print_trainable_vars = False

                if not tflex.child_pid and len(args.fork) > 0:
                  #if tflex.sess.target:
                  #  args.fork = tflex.sess.target + ',' + args.fork
                  while True:
                    if len(args.fork) > 0:
                      target, *more = args.fork.split(',')
                      args.fork = ','.join(more)
                      say('Forking', target)
                      forked = os.fork()
                      if forked <= 0:
                        tflex.child_pid = 1
                        say('Reopening', target)
                        #import pdb
                        #pdb.set_trace()
                        reopen(target)
                        break
                    #tflex.check_commands()
                    #if tflex.should_quit():
                      #return
                    #time.sleep(20.0)
                    break
            except KeyboardInterrupt:
                print('interrupted')
                if args.save_on_ctrlc:
                    save()
                if args.debug_on_ctrlc:
                    import pdb
                    pdb.set_trace()
                else:
                    break

if __name__ == '__main__':
    main()

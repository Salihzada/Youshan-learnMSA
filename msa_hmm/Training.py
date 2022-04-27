import tensorflow as tf
from tensorflow.python.client import device_lib
import numpy as np
from msa_hmm.MsaHmmLayer import MsaHmmLayer
from msa_hmm.AncProbsLayer import AncProbsLayer
import msa_hmm.Utility as ut
import msa_hmm.Fasta as fasta



# def read_seqs(filename, gaps=False):
#     fasta_file = fasta.Fasta(filename, gaps = gaps, contains_lower_case = True)
#     selected_sequences = list(range(len(fasta_file.raw_seq))) #all and in order
#     #one hot sequences with removed gaps, shorter sequences are padded with zeros
#     sequences = fasta_file.one_hot_sequences(subset = selected_sequences)
#     num_seq = sequences.shape[0]
#     len_seq = sequences.shape[1]
#     #replace zero- with end-symbol-padding and make every sequence be succeeded by at least one terminal symbol
#     padding = np.all(sequences==0, -1)
#     padding = np.expand_dims(padding.astype(np.float32), -1)
#     sequences = np.concatenate([sequences, padding], axis=-1)
#     sequences = np.concatenate([sequences, np.expand_dims(np.eye(fasta.s)[[fasta.s-1]*num_seq], 1)], axis=1)
#     am_sequences = np.argmax(sequences, -1) 
#     std_aa_mask = np.expand_dims(am_sequences < 20, -1).astype(np.float32)
#     return sequences, std_aa_mask, fasta_file

def make_model(num_seq,
               model_length, 
               emission_init,
               transition_init,
               flank_init,
               alpha_flank, 
               alpha_single, 
               alpha_frag,
               use_prior=True,
               dirichlet_mix_comp_count=1,
               use_anc_probs=True,
               tau_init=0.0, 
               trainable_kernels={}):
    sequences = tf.keras.Input(shape=(None,fasta.s), name="sequences", dtype=ut.dtype)
    mask = tf.keras.Input(shape=(None,1), name="mask", dtype=ut.dtype)
    subset = tf.keras.Input(shape=(), name="subset", dtype=tf.int32)
    msa_hmm_layer = MsaHmmLayer(length=model_length,
                                num_seq=num_seq,
                                emission_init=emission_init,
                                transition_init=transition_init,
                                flank_init=flank_init,
                                alpha_flank=alpha_flank, 
                                alpha_single=alpha_single, 
                                alpha_frag=alpha_frag,
                                trainable_kernels=trainable_kernels,
                                use_prior=use_prior,
                                dirichlet_mix_comp_count=dirichlet_mix_comp_count)
    anc_probs_layer = AncProbsLayer(num_seq, tau_init=tau_init)
    if use_anc_probs:
        forward_seq = anc_probs_layer(sequences, mask, subset)
    else:
        forward_seq = sequences
    loglik = msa_hmm_layer(forward_seq)
    model = tf.keras.Model(inputs=[sequences, mask, subset], 
                        outputs=[tf.keras.layers.Lambda(lambda x: x, name="loglik")(loglik)])
    return model, msa_hmm_layer, anc_probs_layer  
    
    
    
def make_dataset(fasta_file, batch_size, shuffle=True, indices=None):
    if indices is None:
        indices = tf.range(fasta_file.num_seq)
    def get_seq(i):
        seq = fasta_file.get_raw_seq(i)
        seq = np.append(seq, [fasta.s-1]) #terminal symbol
        seq = seq.astype(np.int32)
        return (seq, tf.cast(i, tf.int64))
    def preprocess_seq(seq, i):
        std_aa_mask = tf.expand_dims(seq < 20, -1)
        std_aa_mask = tf.cast(std_aa_mask, dtype=ut.dtype)
        return tf.one_hot(seq, fasta.s, dtype=ut.dtype), std_aa_mask, i
    ds = tf.data.Dataset.from_tensor_slices(indices)
    if shuffle:
        ds = ds.shuffle(fasta_file.num_seq, reshuffle_each_iteration=True)
        ds = ds.repeat()
    ds = ds.map(lambda i: tf.numpy_function(func=get_seq,
                inp=[i], Tout=(tf.int32, tf.int64)),
                num_parallel_calls=tf.data.AUTOTUNE,
                deterministic=True)
    ds = ds.padded_batch(batch_size, 
                         padded_shapes=([None], []),
                         padding_values=(tf.constant(fasta.s-1, dtype=tf.int32), 
                                         tf.constant(0, dtype=tf.int64)))
    ds = ds.map(preprocess_seq)
    ds_y = tf.data.Dataset.from_tensor_slices(tf.zeros(1)).batch(batch_size).repeat()
    ds = tf.data.Dataset.zip((ds, ds_y))
    ds = ds.prefetch(tf.data.AUTOTUNE) #preprocessings and training steps in parallel
    return ds
    

    
def fit_model(fasta_file, 
              indices, 
              model_length, 
              emission_init,
              transition_init,
              flank_init,
              alpha_flank, 
              alpha_single, 
              alpha_frag,
              use_prior=True,
              dirichlet_mix_comp_count=1,
              use_anc_probs=True,
              tau_init=0.0, 
              trainable_kernels={},
              batch_size=256, 
              learning_rate=0.1,
              epochs=4,
              verbose=True):
    tf.keras.backend.clear_session() #frees occupied memory 
    tf.get_logger().setLevel('ERROR')
    optimizer = tf.optimizers.Adam(learning_rate)
    if verbose:
        print("Fitting a model of length", model_length, "on", indices.shape[0], "sequences.")
        print("Batch size=",batch_size, "Learning rate=",learning_rate)
    def make_and_compile():
        model, msa_hmm_layer, anc_probs_layer = make_model(num_seq=indices.shape[0],
                                                           model_length=model_length, 
                                                           emission_init=emission_init,
                                                           transition_init=transition_init,
                                                           flank_init=flank_init,
                                                           alpha_flank=alpha_flank, 
                                                           alpha_single=alpha_single, 
                                                           alpha_frag=alpha_frag,
                                                           use_prior=use_prior,
                                                           dirichlet_mix_comp_count=dirichlet_mix_comp_count,
                                                           use_anc_probs=use_anc_probs,
                                                           tau_init=tau_init, 
                                                           trainable_kernels=trainable_kernels)
        model.compile(optimizer=optimizer)
        return model, msa_hmm_layer, anc_probs_layer
    num_gpu = len([x.name for x in device_lib.list_local_devices() if x.device_type == 'GPU']) 
    if verbose:
        print("Using", num_gpu, "GPUs.")
    if num_gpu > 1:       
        mirrored_strategy = tf.distribute.MirroredStrategy()    
        with mirrored_strategy.scope():
            model, msa_hmm_layer, anc_probs_layer = make_and_compile()
    else:
         model, msa_hmm_layer, anc_probs_layer = make_and_compile()
    steps = max(30, int(250*np.sqrt(indices.shape[0])/batch_size))
    history = model.fit(make_dataset(fasta_file, batch_size, True, indices), 
                          epochs=epochs,
                          steps_per_epoch=steps,
                          verbose = 2*int(verbose))
    tf.get_logger().setLevel('INFO')
    return model, history
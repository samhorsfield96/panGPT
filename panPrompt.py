import torch
import torch.nn as nn
import math
import torch.nn.functional as F
from transformers import LEDConfig, LEDForConditionalGeneration, LEDTokenizer, logging
import numpy as np
import argparse
from tqdm import tqdm
import re
from tokenizers import Tokenizer
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
from multiprocessing import Manager
import torch.multiprocessing as mp
from panGPT import setup, cleanup

logging.set_verbosity_error()

def parse_args():
    """
    Parse command-line arguments.

    This function parses the command-line arguments provided by the user and returns
    a Namespace object containing the parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Token prediction with a Transformer or Reformer model.")
    parser.add_argument("--model_path", type=str, required=True, help="Path to the model checkpoint file.")
    parser.add_argument("--tokenizer", type=str, default="WordLevel", choices=["WordLevel", "BPE"], help="Tokeniser type to use, WordLevel or BPE")
    parser.add_argument("--tokenizer_path", type=str, required=True, help="Path to the tokenizer file.")
    parser.add_argument("--prompt_file", type=str, required=True, help="Path to the text file containing the prompt.")
    parser.add_argument("--temperature", type=float, default=1.0, help="Temperature for prediction.")
    parser.add_argument("--embed_dim", type=int, default=256, help="Embedding dimension.")
    parser.add_argument("--num_heads", type=int, default=8, help="Number of attention heads.")
    parser.add_argument("--num_layers", type=int, default=8, help="Number of transformer layers.")
    parser.add_argument("--max_seq_length", type=int, default=16384, help="Maximum sequence length.")
    parser.add_argument("--model_dropout_rate", type=float, default=0.2, help="Dropout rate for the model")
    parser.add_argument("--batch_size", type=int, default=16, help="Maximum batch size for simulation. Default = 16")
    parser.add_argument("--device", type=str, default=None, help="Device to run the model on (e.g., 'cpu' or 'cuda').")
    parser.add_argument("--attention_window", type=int, default=512, help="Attention window size in the Longformer model (default: 512)")
    parser.add_argument("--prop_masked", type=float, default=0.3, help="Proportion of prompt to be masked. Default = 0.3")
    parser.add_argument("--num_seq", type=int, default=1, help="Number of simulations per prompt sequence. Default = 1")
    parser.add_argument("--outfile", type=str, default="simulated_genomes.txt", help="Output file for simulated genomes. Default = 'simulated_genomes.txt'")
    parser.add_argument("--DDP", action="store_true", default=False, help="Multiple GPUs used via DDP during training.")

    args = parser.parse_args()

    # Ensure max_seq_length is greater than or equal to attention_window
    args.max_seq_length = max(args.max_seq_length, args.attention_window)
    # Round down max_seq_length to the nearest multiple of attention_window
    args.max_seq_length = (args.max_seq_length // args.attention_window) * args.attention_window

    return args

def pad_blocks(input_list, block_size, pad_token="<pad>"):
    # Initialize the result list
    result = []
    attention_mask = []

    # Iterate over the list in steps of block_size
    for i in range(0, len(input_list), block_size):
        # Get the current block
        block = input_list[i:i + block_size]
        attention_block = [1] * len(block)
        
        # Check if the block needs padding
        if len(block) < block_size:
            # Pad the block to the required block size
            attention_block += [0] * (block_size - len(block))
            block += [pad_token] * (block_size - len(block))            
        
        # Add the block to the result
        result.append(block)
        attention_mask.append(attention_block)
    
    return result, attention_mask

# returns list first entry is encoded, second is attention mask
def tokenize_prompt(prompt, max_seq_length, tokenizer, tokenizer_type):
    if tokenizer_type == "BPE":
        mask_token = tokenizer.mask_token_id
        pad_token = tokenizer.pad_token_id
    else:
        mask_token = tokenizer.encode("<mask>").ids[0]
        pad_token = tokenizer.encode("<pad>").ids[0]

    if tokenizer_type == "BPE":
        encoded = tokenizer.encode(prompt)
    else:
        encoded = tokenizer.encode(prompt).ids

    # merge consecutive masks into single mask token
    encoder_input = ' '.join([str(i) for i in encoded])
    #print('encoder_input pre merging')
    #print(encoder_input)
    pattern = f'({mask_token} )+'
    encoder_input = re.sub(pattern, str(mask_token) + ' ', encoder_input)
    pattern = f'( {mask_token})+'
    encoder_input = re.sub(pattern, ' ' + str(mask_token), encoder_input)

    encoder_input = [int(i) for i in encoder_input.split()]

    #print('encoder_input post merging')
    #print(encoder_input)

    encoder_input, attention_mask = pad_blocks(encoder_input, max_seq_length, pad_token)

    #print('encoder_input post padding')
    #print(encoder_input)

    return encoder_input, attention_mask

def print_banner():
    banner = '''
    **************************************************
    *                                                *
    *        Transformer Model Token Prediction      *
    *        panPrompt v0.01                         *
    *        author: James McInerney                 *
    *                                                *
    **************************************************
    '''
    print(banner)

def load_model(embed_dim, num_heads, num_layers, max_seq_length, device, vocab_size, attention_window, model_dropout_rate):

    BARTlongformer_config = LEDConfig(
        vocab_size=vocab_size,
        d_model=embed_dim,
        encoder_layers=num_layers,
        decoder_layers=num_layers,
        encoder_attention_heads=num_heads,
        decoder_attention_heads=num_heads,
        decoder_ffn_dim=4 * embed_dim,
        encoder_ffn_dim=4 * embed_dim,
        max_encoder_position_embeddings=max_seq_length,
        max_decoder_position_embeddings=max_seq_length,
        dropout=model_dropout_rate,
        attention_window = attention_window
        )
    model = LEDForConditionalGeneration(BARTlongformer_config)
    return model

def mask_integers(string, prop_masked):   
    # Identify the indices of the integers in the list
    integer_indices = np.array(string.split())
    
    # Determine how many integers to mask
    num_to_mask = int(len(integer_indices) * prop_masked)
    
    # Randomly select indices to mask
    if num_to_mask > 0:
        indices_to_mask = np.random.choice(range(len(integer_indices)), size=num_to_mask, replace=False)
    else:
        indices_to_mask = np.empty(shape=[0, 0])
    
    # Replace selected indices with "[MASK]"
    integer_indices[indices_to_mask] = "<mask>"

    # Reconstruct the string
    masked_string = ' '.join(integer_indices.tolist())
    
    return masked_string

def predict_next_tokens_BART(model, tokenizer, input_ids, attention_mask, device, batch_size, temperature, DDP_active):
    model.eval()

    output = ""
    num_batches = len(input_ids) // batch_size + (1 if len(input_ids) % batch_size != 0 else 0)

    for batch_index in range(num_batches):
        start_index = batch_index * batch_size
        end_index = min(start_index + batch_size, len(input_ids))

        # Stack input_ids and attention_mask for the current batch
        batch_input_ids = torch.cat(input_ids[start_index:end_index], dim=0).to(device)
        batch_attention_mask = torch.cat(attention_mask[start_index:end_index], dim=0).to(device)

        # Ensure all tokens attend globally just to the first token if first batch
        #global_attention_mask = torch.zeros(batch_input_ids.shape, dtype=torch.long, device=batch_input_ids.device)
        #if batch_index == 0:
            #global_attention_mask[0, 0] = 1
        
        #print(batch_attention_mask)
        #print(batch_input_ids)

        # Generate summaries for the current batch
        if DDP_active:
            summary_ids = model.module.generate(
                batch_input_ids,
                #global_attention_mask=global_attention_mask,
                attention_mask=batch_attention_mask,
                # is max length here correct?
                max_length=batch_input_ids.shape[1],
                temperature=temperature,
                do_sample=True
            )
        else:
            summary_ids = model.generate(
                batch_input_ids,
                #global_attention_mask=global_attention_mask,
                attention_mask=batch_attention_mask,
                # is max length here correct?
                max_length=batch_input_ids.shape[1],
                temperature=temperature,
                do_sample=True
            )

        # Decode the generated summaries
        for summary in summary_ids:
            decoded = tokenizer.decode(summary.tolist(), skip_special_tokens=True)
            output += decoded

    return output

def read_prompt_file(file_path):
    prompt_list = []
    with open(file_path, 'r') as file:
        for line in file:
            prompt_list.append(line.strip())
    return prompt_list

def split_prompts(prompts, world_size):
    # Split prompts into approximately equal chunks for each GPU
    chunk_size = len(prompts) // world_size
    return [prompts[i * chunk_size:(i + 1) * chunk_size] for i in range(world_size)]

def query_model(rank, model_path, world_size, args, BARTlongformer_config, tokenizer, prompt_list, prop_masked, num_seq, DDP_active, return_list):
    if DDP_active:
        setup(rank, world_size)
        prompt_list = prompt_list[rank]
    
    model = LEDForConditionalGeneration(BARTlongformer_config)
    device = rank
    model = model.to(device)
    if DDP_active:
        model = DDP(model, device_ids=[rank], find_unused_parameters=True)

    map_location = None
    if DDP_active:
        map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
        dist.barrier()
    
    if map_location != None:
        checkpoint = torch.load(model_path, map_location=map_location)
    else:
        checkpoint = torch.load(model_path)
    model.load_state_dict(checkpoint["model_state_dict"])

    master_process = rank == 0
    for prompt in tqdm(prompt_list, desc="Prompt number", total=len(prompt_list), disable=not master_process):
        if prop_masked > 0:
            prompt = mask_integers(prompt, prop_masked)

        tokens, attention_mask = tokenize_prompt(prompt, args.max_seq_length, tokenizer, args.tokenizer)

        #print(tokens)
        #print(attention_mask)
        input_ids = [torch.tensor([input]) for input in tokens]
        attention_mask = [torch.tensor([input], dtype=torch.long) for input in attention_mask]
        
        #print(prompt)
        for _ in range(num_seq):
            predicted_text = predict_next_tokens_BART(model, tokenizer, input_ids, attention_mask, device, args.batch_size, args.temperature, DDP_active)
            #print(predicted_text)
            return_list.append(predicted_text)
        
def main():
    print_banner()
    args = parse_args()

    if args.tokenizer == "BPE":
        tokenizer = LEDTokenizer.from_pretrained(args.tokenizer_path, add_prefix_space=True)
        vocab_size = tokenizer.vocab_size
    elif args.tokenizer == "WordLevel":
        tokenizer = Tokenizer.from_file(args.tokenizer_path)
        vocab_size = tokenizer.get_vocab_size()

    args.max_seq_length = max(args.max_seq_length, args.attention_window)
    # Round down max_seq_length to the nearest multiple of attention_window
    args.max_seq_length = (args.max_seq_length // args.attention_window) * args.attention_window
    device = args.device

    DDP_active = args.DDP

    BARTlongformer_config = LEDConfig(
        vocab_size=vocab_size,
        d_model=args.embed_dim,
        encoder_layers=args.num_layers,
        decoder_layers=args.num_layers,
        encoder_attention_heads=args.num_heads,
        decoder_attention_heads=args.num_heads,
        decoder_ffn_dim=4 * args.embed_dim,
        encoder_ffn_dim=4 * args.embed_dim,
        max_encoder_position_embeddings=args.max_seq_length,
        max_decoder_position_embeddings=args.max_seq_length,
        dropout=args.model_dropout_rate,
        attention_window = args.attention_window
        )
    
    world_size = torch.cuda.device_count()
    if DDP_active:
        if world_size > 0:
            # Use DDP but just one GPU
            if device != None:
                device = torch.device("cuda:{}".format(device))
                world_size = 1
            else:
                device = torch.device("cuda") # Run on a GPU if one is available
            print("{} GPU(s) available, using cuda".format(world_size))
        else:
            print("GPU not available, using cpu.")
            device = torch.device("cpu")
    else:
        if world_size > 0 and device != "cpu":
            device = torch.device("cuda:{}".format(device))
        else:
            device = torch.device("cpu")

    prompt_list = read_prompt_file(args.prompt_file)
    #if args.model_type == "BARTlongformer":

    return_list = []
    if DDP_active:
        prompt_list = split_prompts(prompt_list, world_size)
        with Manager() as manager:
            mp_list = manager.list()
            mp.spawn(query_model,
                    args=(args.model_path, world_size, args, BARTlongformer_config, tokenizer, prompt_list, args.prop_masked, args.num_seq, DDP_active, mp_list),
                    nprocs=world_size,
                    join=True)
            return_list = list(mp_list)
    else:
        query_model(args.model_path, 1, args, BARTlongformer_config, tokenizer, prompt_list, args.prop_masked, args.num_seq, DDP_active, return_list)
    
    with open(args.outfile, "w") as f:
        for entry in return_list:
            f.write(entry + "\n")

    if DDP_active:
        cleanup()

if __name__ == "__main__":
    main()

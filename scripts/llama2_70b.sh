#!/bin/bash

# Set common variables
model="meta-llama/Llama-2-70b-hf"
sparsity_ratio=0.7
cuda_device=0,1,2,3,4,5,6,7

# Set CUDA device visibility
export CUDA_VISIBLE_DEVICES=$cuda_device

# Define function to run python command
run_python_command () {
    python  main.py \
    --model $model \
    --cache_dir llm_weights/ \
    --prune_method $1 \
    --sparsity_ratio $sparsity_ratio \
    --save $3 \
    --ww_metric alpha_peak \
    --ww_metric_cache ./data/llama2-70b-hf/ \
    --epsilon $2
}


# llama2-70b with magnitude pruning method
echo "Running with magnitude pruning method"
run_python_command "magnitude_ww" 0.4 "results/llama2_70b/"
echo "Finished magnitude pruning method"

# llama2-70b with wanda pruning method
echo "Running with wanda pruning method"
run_python_command "wanda_ww" 0.3 "results/llama2_70b/"
echo "Finished wanda pruning method"

# llama2-70b with sparsegpt pruning method
echo "Running with sparsegpt pruning method"
run_python_command "sparsegpt_ww" 0.3 "results/llama2_70b/"
echo "Finished sparsegpt pruning method"
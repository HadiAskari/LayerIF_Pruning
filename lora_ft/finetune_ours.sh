python finetune_lm.py \
    --model_name_or_path [Path to Sparse model weights] \
    --config_name "pinkmanlove/llama-7b-hf" \
    --dataset_name c4 \
    --num_train_epochs 1 \
    --block_size 2048 \
    --per_device_train_batch_size 1 \
    --per_device_eval_batch_size 8 \
    --do_train \
    --max_train_samples 100000 \
    --max_eval_samples 128 \
    --learning_rate 1e-4 \
    --output_dir [Path to save Lora weights] \
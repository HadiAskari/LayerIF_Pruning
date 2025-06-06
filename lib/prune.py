import time
import heapq
import torch
import torch.nn as nn
from .sparsegpt import SparseGPT 
from .layerwrapper import WrappedGPT
from .data import get_loaders 
import numpy as np
from collections import defaultdict

import os

from .utils import get_weights, get_modules


def find_layers(module, layers=[nn.Linear], name=''):
    """
    Recursively find the layers of a certain type in a module.

    Args:
        module (nn.Module): PyTorch module.
        layers (list): List of layer types to find.
        name (str): Name of the module.

    Returns:
        dict: Dictionary of layers of the given type(s) within the module.
    """
    
    if type(module) in layers:
        return {name: module}
    res = {}
    for name1, child in module.named_children():
        res.update(find_layers(
            child, layers=layers, name=name + '.' + name1 if name != '' else name1
        ))
    return res

def check_sparsity(model):
    use_cache = model.config.use_cache 
    model.config.use_cache = False 


    if "OPT" in model.__class__.__name__:
        layers = model.model.decoder.layers
    else:    
        layers = model.model.layers  
        
        
    count = 0 
    total_params = 0
    for i in range(len(layers)):
        layer = layers[i]
        subset = find_layers(layer)

        sub_count = 0
        sub_params = 0
        for name in subset:
            W = subset[name].weight.data
            count += (W==0).sum().item()
            total_params += W.numel()

            sub_count += (W==0).sum().item()
            sub_params += W.numel()

        print(f"layer {i} sparsity {float(sub_count)/sub_params:.6f}")

    model.config.use_cache = use_cache 
    return float(count)/total_params 

def prepare_calibration_input(model, dataloader, device):
    layers = model.model.layers

    # dev = model.hf_device_map["model.embed_tokens"]
    if "model.embed_tokens" in model.hf_device_map:
        device = model.hf_device_map["model.embed_tokens"]

    dtype = next(iter(model.parameters())).dtype
    inps = torch.zeros((128, model.seqlen, model.config.hidden_size), dtype=dtype, device=device)
    inps.requires_grad = False
    cache = {'i': 0, 'attention_mask': None, "position_ids": None}

    class Catcher(nn.Module):
        def __init__(self, module):
            super().__init__()
            self.module = module
        def forward(self, inp, **kwargs):
            inps[cache['i']] = inp
            cache['i'] += 1
            cache['attention_mask'] = kwargs['attention_mask']
            cache['position_ids'] = kwargs['position_ids']
            raise ValueError
    layers[0] = Catcher(layers[0])
    for batch in dataloader:
        try:
            model(batch[0].to(device))
        except ValueError:
            pass 

    layers[0] = layers[0].module
    torch.cuda.empty_cache()
    outs = torch.zeros_like(inps)
    attention_mask = cache['attention_mask']
    position_ids = cache['position_ids']

    return inps, outs, attention_mask, position_ids

def prepare_calibration_input_opt(model, dataloader, device):
    layers = model.model.decoder.layers
    
    if "model.embed_tokens" in model.hf_device_map:
        device = model.hf_device_map["model.embed_tokens"]

    dtype = next(iter(model.parameters())).dtype
    inps = torch.zeros((128, model.seqlen, model.config.hidden_size), dtype=dtype, device=device)
    inps.requires_grad = False
    cache = {'i': 0, 'attention_mask': None}
    
    class Catcher(nn.Module):
        def __init__(self, module):
            super().__init__()
            self.module = module
        def forward(self, inp, **kwargs):
            inps[cache['i']] = inp
            cache['i'] += 1
            cache['attention_mask'] = kwargs['attention_mask']
            raise ValueError
        
    layers[0] = Catcher(layers[0])
    for batch in dataloader:
        try:
            model(batch[0].to(device))
        except ValueError:
            pass 
        
    layers[0] = layers[0].module
    torch.cuda.empty_cache()
    outs = torch.zeros_like(inps)
    attention_mask = cache['attention_mask']

    return inps, outs, attention_mask, None


def return_given_alpha(alpha, sort_res, W_metric, tmp_metric, sum_before):
    thres_cumsum = sum_before * alpha 
    sort_mask = tmp_metric <= thres_cumsum.reshape((-1,1))
    thres = torch.gather(sort_res[0], dim=1, index=sort_mask.sum(dim=1, keepdims=True)-1)
    W_mask = (W_metric <= thres)
    cur_sparsity = (W_mask==True).sum() / W_mask.numel()
    return W_mask, cur_sparsity


def ww_sparsity(args, model, device=torch.device("cuda:0"), s1=0.8, s2=1.2, ratios=None, prune_n=0, prune_m=0):
    if "opt" in args.model:
        blocks = model.model.decoder.layers    
    else:
        blocks = model.model.layers
    
    layers = [find_layers(blocks)]
    prunables = []
    for layer in layers:
        for name in layer:
            prunables.append(layer[name].weight.numel())

    layer_num_in_block = int(len(prunables) / len(blocks))

    metrics = np.load(f"{args.ww_metric_cache}/{args.ww_metric}.npy")
    # print("Printing Metrics")
    # print(metrics)
    # print(len(metrics))
    # print("XXXXXXXXXXX")
    
    if args.mapping_type == 'block_wise':
        block_metrics = [np.mean(metrics[i:i+layer_num_in_block]) for i in range(0, len(metrics), layer_num_in_block)]
        # print("Printing Block Metrics")
        # print(block_metrics)
        # print(len(block_metrics))
        # print("XXXXXXXXXXX")
        metrics = [i for i in block_metrics for j in range(layer_num_in_block)]
 
    # print("metric values:", metrics)
    
    # print(len(metrics))
    # print("XXXXXXXXXXX")
            
    scores = torch.tensor(metrics)
    prunables = torch.tensor(prunables)

    # #log
    # epsilon = 1e-8
    # scores = torch.log(scores + epsilon)
    
    # linear mapping
    max = torch.max(scores)
    min = torch.min(scores)
    
    layerwise_pruning_ratios = (((scores - min) / (max - min)) * (s2 - s1) + s1)
    scaler = torch.sum(prunables) * args.sparsity_ratio / (torch.sum(prunables * layerwise_pruning_ratios))  
    layerwise_pruning_ratios = layerwise_pruning_ratios * scaler
    layerwise_pruning_ratios = layerwise_pruning_ratios.cpu().numpy().tolist()
    
    #hardcoding for OWL
    layerwise_pruning_ratios=[0.2992475233217756, 0.2992475233217756, 0.2992475233217756, 0.2992475233217756, 0.2992475233217756, 0.2992475233217756, 0.2992475233217756, 0.37553422105374656, 0.37553422105374656, 0.37553422105374656, 0.37553422105374656, 0.37553422105374656, 0.37553422105374656, 0.37553422105374656, 0.3096085855840248, 0.3096085855840248, 0.3096085855840248, 0.3096085855840248, 0.3096085855840248, 0.3096085855840248, 0.3096085855840248, 0.39912727912968515, 0.39912727912968515, 0.39912727912968515, 0.39912727912968515, 0.39912727912968515, 0.39912727912968515, 0.39912727912968515, 0.37049639484876185, 0.37049639484876185, 0.37049639484876185, 0.37049639484876185, 0.37049639484876185, 0.37049639484876185, 0.37049639484876185, 0.363119958698063, 0.363119958698063, 0.363119958698063, 0.363119958698063, 0.363119958698063, 0.363119958698063, 0.363119958698063, 0.3832077393432515, 0.3832077393432515, 0.3832077393432515, 0.3832077393432515, 0.3832077393432515, 0.3832077393432515, 0.3832077393432515, 0.3968031473086051, 0.3968031473086051, 0.3968031473086051, 0.3968031473086051, 0.3968031473086051, 0.3968031473086051, 0.3968031473086051, 0.39036700362444143, 0.39036700362444143, 0.39036700362444143, 0.39036700362444143, 0.39036700362444143, 0.39036700362444143, 0.39036700362444143, 0.36762467566265133, 0.36762467566265133, 0.36762467566265133, 0.36762467566265133, 0.36762467566265133, 0.36762467566265133, 0.36762467566265133, 0.3692726004998601, 0.3692726004998601, 0.3692726004998601, 0.3692726004998601, 0.3692726004998601, 0.3692726004998601, 0.3692726004998601, 0.35804071832261397, 0.35804071832261397, 0.35804071832261397, 0.35804071832261397, 0.35804071832261397, 0.35804071832261397, 0.35804071832261397, 0.3627425173558624, 0.3627425173558624, 0.3627425173558624, 0.3627425173558624, 0.3627425173558624, 0.3627425173558624, 0.3627425173558624, 0.3827980869795785, 0.3827980869795785, 0.3827980869795785, 0.3827980869795785, 0.3827980869795785, 0.3827980869795785, 0.3827980869795785, 0.37866924008785086, 0.37866924008785086, 0.37866924008785086, 0.37866924008785086, 0.37866924008785086, 0.37866924008785086, 0.37866924008785086, 0.3819340010760389, 0.3819340010760389, 0.3819340010760389, 0.3819340010760389, 0.3819340010760389, 0.3819340010760389, 0.3819340010760389, 0.39643928622284386, 0.39643928622284386, 0.39643928622284386, 0.39643928622284386, 0.39643928622284386, 0.39643928622284386, 0.39643928622284386, 0.40876443516023786, 0.40876443516023786, 0.40876443516023786, 0.40876443516023786, 0.40876443516023786, 0.40876443516023786, 0.40876443516023786, 0.3928030322692426, 0.3928030322692426, 0.3928030322692426, 0.3928030322692426, 0.3928030322692426, 0.3928030322692426, 0.3928030322692426, 0.4172027126842822, 0.4172027126842822, 0.4172027126842822, 0.4172027126842822, 0.4172027126842822, 0.4172027126842822, 0.4172027126842822, 0.4338660240362121, 0.4338660240362121, 0.4338660240362121, 0.4338660240362121, 0.4338660240362121, 0.4338660240362121, 0.4338660240362121, 0.44178679277973676, 0.44178679277973676, 0.44178679277973676, 0.44178679277973676, 0.44178679277973676, 0.44178679277973676, 0.44178679277973676, 0.44493954471067954, 0.44493954471067954, 0.44493954471067954, 0.44493954471067954, 0.44493954471067954, 0.44493954471067954, 0.44493954471067954, 0.4527467331276187, 0.4527467331276187, 0.4527467331276187, 0.4527467331276187, 0.4527467331276187, 0.4527467331276187, 0.4527467331276187, 0.4552465104142188, 0.4552465104142188, 0.4552465104142188, 0.4552465104142188, 0.4552465104142188, 0.4552465104142188, 0.4552465104142188, 0.45924752332177565, 0.45924752332177565, 0.45924752332177565, 0.45924752332177565, 0.45924752332177565, 0.45924752332177565, 0.45924752332177565, 0.4543835468459224, 0.4543835468459224, 0.4543835468459224, 0.4543835468459224, 0.4543835468459224, 0.4543835468459224, 0.4543835468459224, 0.4541995960996045, 0.4541995960996045, 0.4541995960996045, 0.4541995960996045, 0.4541995960996045, 0.4541995960996045, 0.4541995960996045, 0.43029722243071167, 0.43029722243071167, 0.43029722243071167, 0.43029722243071167, 0.43029722243071167, 0.43029722243071167, 0.43029722243071167, 0.4275402059064296, 0.4275402059064296, 0.4275402059064296, 0.4275402059064296, 0.4275402059064296, 0.4275402059064296, 0.4275402059064296, 0.41992448788193604, 0.41992448788193604, 0.41992448788193604, 0.41992448788193604, 0.41992448788193604, 0.41992448788193604, 0.41992448788193604, 0.4220186532117377, 0.4220186532117377, 0.4220186532117377, 0.4220186532117377, 0.4220186532117377, 0.4220186532117377, 0.4220186532117377]
    
    print(layerwise_pruning_ratios)
    save_filepath = os.path.join(args.save, f"prune_ratios_{args.prune_method}_sparsity_{args.sparsity_ratio}_epsilon_{args.epsilon}.txt")
    with open(save_filepath, "w") as f:
        f.write(f"{layerwise_pruning_ratios}")
    for k,v in enumerate(layerwise_pruning_ratios):
        if v>0.99:
            layerwise_pruning_ratios[k]=0.99
            print(k)
    return layerwise_pruning_ratios

#########################################################################################################################

def prune_magnitude(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0, ratios=None):
    if "OPT" in model.__class__.__name__:
        layers = model.model.decoder.layers
    else:    
        layers = model.model.layers
    
    layer_num = len(find_layers(layers))
    if ratios is None:
        ratios = [args.sparsity_ratio for i in range(layer_num)]

    k=0
    for i in range(len(layers)):
        layer = layers[i]
        subset = find_layers(layer)

        for name in subset:
            W = subset[name].weight.data 
            W_metric = torch.abs(W)
            print(ratios[k])
            thresh = torch.sort(W_metric.flatten().cuda())[0][int(W.numel()*ratios[k])].cpu()
            W_mask = (W_metric<=thresh)
            k+=1

            W[W_mask] = 0
    

def prune_wanda(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0, ratios=None):
    use_cache = model.config.use_cache 
    model.config.use_cache = False 

    print("loading calibdation data")
    dataloader, _ = get_loaders("c4",nsamples=args.nsamples,seed=args.seed,seqlen=model.seqlen,tokenizer=tokenizer)
    print("dataset loading complete")
    with torch.no_grad():
        if "OPT" in model.__class__.__name__:
            inps, outs, attention_mask, position_ids = prepare_calibration_input_opt(model, dataloader, device)
        else:
            inps, outs, attention_mask, position_ids = prepare_calibration_input(model, dataloader, device)


    print ("inps",inps)

    if "OPT" in model.__class__.__name__:
        layers = model.model.decoder.layers
    else:    
        layers = model.model.layers
    
    layer_num = len(find_layers(layers))
    if ratios is None:
        ratios = [args.sparsity_ratio for i in range(layer_num)]
    
    k=0
    for i in range(len(layers)):
        layer = layers[i]
        subset = find_layers(layer)

        # if hasattr(model, "hf_device_map"):
        #     unique_devices = set(model.hf_device_map.values())
        #     if len(unique_devices) > 1:
        #         print(f"Model is sharded across multiple devices: {unique_devices}")
        #     else:
        #         print(f"Model is on a single device: {unique_devices.pop()}")
        # else:
        #     print("hf_device_map not found. Checking parameter devices...")
        #     check_model_devices(model)

        
        if f"model.layers.{i}" in model.hf_device_map:
            dev = model.hf_device_map[f"model.layers.{i}"]
            print(dev)
            if inps is None:
                print("Inps is None")
            if outs is None:
                print("Outs is None")
            if attention_mask is None:
                print("Attention_Mask is None")
            if position_ids is None:
                print("Position IDs is None")
            inps, outs, attention_mask, position_ids = inps.to(dev), outs.to(dev), attention_mask.to(dev), position_ids.to(dev)
        else:
            dev = "Not Found"

        

        wrapped_layers = {}
        for name in subset:
            wrapped_layers[name] = WrappedGPT(subset[name])

        def add_batch(name):
            def tmp(_, inp, out):
                wrapped_layers[name].add_batch(inp[0].data, out.data)
            return tmp

        handles = []
        for name in wrapped_layers:
            handles.append(subset[name].register_forward_hook(add_batch(name)))
        for j in range(args.nsamples):
            with torch.no_grad():
                if "OPT" in model.__class__.__name__:
                    outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
                else:
                    outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]
        for h in handles:
            h.remove()

        for name in subset:
            print(f"pruning layer {i} name {name}")
            W_metric = torch.abs(subset[name].weight.data) * torch.sqrt(wrapped_layers[name].scaler_row.reshape((1,-1)))

            W_mask = (torch.zeros_like(W_metric) == 1)  ## initialize a mask to be all False
            if prune_n != 0:
                # structured n:m sparsity
                for ii in range(W_metric.shape[1]):
                    if ii % prune_m == 0:
                        tmp = W_metric[:,ii:(ii+prune_m)].float()
                        W_mask.scatter_(1,ii+torch.topk(tmp, prune_n,dim=1, largest=False)[1], True)
            else:
                sort_res = torch.sort(W_metric, dim=-1, stable=True)

                if args.use_variant:
                    # wanda variant 
                    tmp_metric = torch.cumsum(sort_res[0], dim=1)
                    sum_before = W_metric.sum(dim=1)

                    alpha = 0.4
                    alpha_hist = [0., 0.8]
                    W_mask, cur_sparsity = return_given_alpha(alpha, sort_res, W_metric, tmp_metric, sum_before)
                    while (torch.abs(cur_sparsity - args.sparsity_ratio)>0.001) and (alpha_hist[1]-alpha_hist[0]>=0.001):
                        if cur_sparsity > args.sparsity_ratio:
                            alpha_new = (alpha + alpha_hist[0]) / 2.0
                            alpha_hist[1] = alpha
                        else:
                            alpha_new = (alpha + alpha_hist[1]) / 2.0
                            alpha_hist[0] = alpha

                        alpha = alpha_new 
                        W_mask, cur_sparsity = return_given_alpha(alpha, sort_res, W_metric, tmp_metric, sum_before)
                    print(f"alpha found {alpha} sparsity {cur_sparsity:.6f}")
                else:
                    # unstructured pruning
                    indices = sort_res[1][:,:int(W_metric.shape[1]*ratios[k])]
                    k+=1
                    W_mask.scatter_(1, indices, True)
#             print ("W_mask",W_mask)
            subset[name].weight.data[W_mask] = 0  ## set weights to zero 

        for j in range(args.nsamples):
            with torch.no_grad():
                if "OPT" in model.__class__.__name__:
                    outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
                else:
                    outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]
        inps, outs = outs, inps

    model.config.use_cache = use_cache 
    torch.cuda.empty_cache()


@torch.no_grad()
def prune_sparsegpt(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0, ratios=None):
    ## SparseGPT code available at: https://github.com/IST-DASLab/sparsegpt/tree/f5c25005a61f96a0933ca2f95705a963585aafaa
    print('Starting ...')
    use_cache = model.config.use_cache
    model.config.use_cache = False
    
    dataloader, _ = get_loaders("c4",nsamples=args.nsamples,seed=args.seed,seqlen=model.seqlen,tokenizer=tokenizer)
    
    with torch.no_grad():
        if "OPT" in model.__class__.__name__:
            inps, outs, attention_mask, position_ids = prepare_calibration_input_opt(model, dataloader, device)
        else:
            inps, outs, attention_mask, position_ids = prepare_calibration_input(model, dataloader, device)

    if "OPT" in model.__class__.__name__:
        layers = model.model.decoder.layers
    else:    
        layers = model.model.layers        
    
    
    layer_num = len(find_layers(layers))
    if ratios is None:
        ratios = [args.sparsity_ratio for i in range(layer_num)]
    
    print('Ready.')
    k=0
    for i in range(len(layers)):
        layer = layers[i]
        if f"model.layers.{i}" in model.hf_device_map:
            print("using !")
            dev = model.hf_device_map[f"model.layers.{i}"]
            print(f"layer {i} device {dev}")
            inps, outs, attention_mask, position_ids = inps.to(dev), outs.to(dev), attention_mask.to(dev), position_ids.to(dev)

        subset = find_layers(layer)

        gpts = {}
        for name in subset:
            gpts[name] = SparseGPT(subset[name])

        def add_batch(name):
            def tmp(_, inp, out):
                gpts[name].add_batch(inp[0].data, out.data)
            return tmp

        handles = []
        for name in gpts:
            handles.append(subset[name].register_forward_hook(add_batch(name)))

        for j in range(args.nsamples):
            if "OPT" in model.__class__.__name__:
                outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
            else:
                outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]
        for h in handles:
            h.remove()

        for name in gpts:
            print(i, name)
            print('Pruning ...')

            gpts[name].fasterprune(ratios[k], prune_n=prune_n, prune_m=prune_m, percdamp=0.01, blocksize=128)
            gpts[name].free()
            k+=1

        for j in range(args.nsamples):
            if "OPT" in model.__class__.__name__:
                outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
            else:
                outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]

        layers[i] = layer 
        torch.cuda.empty_cache()

        inps, outs = outs, inps

    model.config.use_cache = use_cache
    torch.cuda.empty_cache()

###############################################################################################################
def prune_magnitude_ww(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0):
    s1 = 1.0 - args.epsilon
    s2 = 1.0 + args.epsilon
    
    all_layer_ratio = ww_sparsity(args, model, device, s1, s2)
    # magnitude pruning
    prune_magnitude(args, model, tokenizer, device, ratios=all_layer_ratio)
    

def prune_wanda_ww(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0):
    s1 = 1.0 - args.epsilon
    s2 = 1.0 + args.epsilon

    all_layer_ratio = ww_sparsity(args, model, device, s1, s2)
    # wanda pruning
    prune_wanda(args, model, tokenizer, device, ratios=all_layer_ratio)
    
def prune_sparsegpt_ww(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0):
    s1 = 1.0 - args.epsilon
    s2 = 1.0 + args.epsilon

    all_layer_ratio = ww_sparsity(args, model, device, s1, s2)
    # sparsegpt pruning
    prune_sparsegpt(args, model, tokenizer, device, ratios=all_layer_ratio)

# import time
# import heapq
# import torch
# import torch.nn as nn
# from .sparsegpt import SparseGPT 
# from .layerwrapper import WrappedGPT
# from .data import get_loaders 
# import numpy as np
# from collections import defaultdict

# import os

# from .utils import get_weights, get_modules


# def find_layers(module, layers=[nn.Linear], name=''):
#     """
#     Recursively find the layers of a certain type in a module.

#     Args:
#         module (nn.Module): PyTorch module.
#         layers (list): List of layer types to find.
#         name (str): Name of the module.

#     Returns:
#         dict: Dictionary of layers of the given type(s) within the module.
#     """
    
#     if type(module) in layers:
#         return {name: module}
#     res = {}
#     for name1, child in module.named_children():
#         res.update(find_layers(
#             child, layers=layers, name=name + '.' + name1 if name != '' else name1
#         ))
#     return res

# def check_sparsity(model):
#     use_cache = model.config.use_cache 
#     model.config.use_cache = False 


#     if "OPT" in model.__class__.__name__:
#         layers = model.model.decoder.layers
#     else:    
#         layers = model.model.layers  
        
        
#     count = 0 
#     total_params = 0
#     for i in range(len(layers)):
#         layer = layers[i]
#         subset = find_layers(layer)

#         sub_count = 0
#         sub_params = 0
#         for name in subset:
#             W = subset[name].weight.data
#             count += (W==0).sum().item()
#             total_params += W.numel()

#             sub_count += (W==0).sum().item()
#             sub_params += W.numel()

#         print(f"layer {i} sparsity {float(sub_count)/sub_params:.6f}")

#     model.config.use_cache = use_cache 
#     return float(count)/total_params 

# def prepare_calibration_input(model, dataloader, device):
#     layers = model.model.layers

#     # dev = model.hf_device_map["model.embed_tokens"]
#     if "model.embed_tokens" in model.hf_device_map:
#         device = model.hf_device_map["model.embed_tokens"]

#     dtype = next(iter(model.parameters())).dtype
#     inps = torch.zeros((128, model.seqlen, model.config.hidden_size), dtype=dtype, device=device)
#     inps.requires_grad = False
#     cache = {'i': 0, 'attention_mask': None, "position_ids": None}

#     class Catcher(nn.Module):
#         def __init__(self, module):
#             super().__init__()
#             self.module = module
#         def forward(self, inp, **kwargs):
#             inps[cache['i']] = inp
#             cache['i'] += 1
#             cache['attention_mask'] = kwargs['attention_mask']
#             cache['position_ids'] = kwargs['position_ids']
#             raise ValueError
#     layers[0] = Catcher(layers[0])
#     for batch in dataloader:
#         try:
#             model(batch[0].to(device))
#         except ValueError:
#             pass 

#     layers[0] = layers[0].module
#     torch.cuda.empty_cache()
#     outs = torch.zeros_like(inps)
#     attention_mask = cache['attention_mask']
#     position_ids = cache['position_ids']

#     return inps, outs, attention_mask, position_ids

# def prepare_calibration_input_opt(model, dataloader, device):
#     layers = model.model.decoder.layers
    
#     if "model.embed_tokens" in model.hf_device_map:
#         device = model.hf_device_map["model.embed_tokens"]

#     dtype = next(iter(model.parameters())).dtype
#     inps = torch.zeros((128, model.seqlen, model.config.hidden_size), dtype=dtype, device=device)
#     inps.requires_grad = False
#     cache = {'i': 0, 'attention_mask': None}
    
#     class Catcher(nn.Module):
#         def __init__(self, module):
#             super().__init__()
#             self.module = module
#         def forward(self, inp, **kwargs):
#             inps[cache['i']] = inp
#             cache['i'] += 1
#             cache['attention_mask'] = kwargs['attention_mask']
#             raise ValueError
        
#     layers[0] = Catcher(layers[0])
#     for batch in dataloader:
#         try:
#             model(batch[0].to(device))
#         except ValueError:
#             pass 
        
#     layers[0] = layers[0].module
#     torch.cuda.empty_cache()
#     outs = torch.zeros_like(inps)
#     attention_mask = cache['attention_mask']

#     return inps, outs, attention_mask, None


# def return_given_alpha(alpha, sort_res, W_metric, tmp_metric, sum_before):
#     thres_cumsum = sum_before * alpha 
#     sort_mask = tmp_metric <= thres_cumsum.reshape((-1,1))
#     thres = torch.gather(sort_res[0], dim=1, index=sort_mask.sum(dim=1, keepdims=True)-1)
#     W_mask = (W_metric <= thres)
#     cur_sparsity = (W_mask==True).sum() / W_mask.numel()
#     return W_mask, cur_sparsity


# def ww_sparsity(args, model, device=torch.device("cuda:0"), s1=0.8, s2=1.2, ratios=None, prune_n=0, prune_m=0):
#     if "opt" in args.model:
#         blocks = model.model.decoder.layers    
#     else:
#         blocks = model.model.layers
    
#     layers = [find_layers(blocks)]
#     prunables = []
#     for layer in layers:
#         for name in layer:
#             prunables.append(layer[name].weight.numel())

#     layer_num_in_block = int(len(prunables) / len(blocks))

#     metrics = np.load(f"{args.ww_metric_cache}/{args.ww_metric}.npy")
    
#     if args.mapping_type == 'block_wise':
#         block_metrics = [np.mean(metrics[i:i+layer_num_in_block]) for i in range(0, len(metrics), layer_num_in_block)]
#         metrics = [i for i in block_metrics for j in range(layer_num_in_block)]
    
#     print("metric values:", metrics)
            
#     scores = torch.tensor(metrics)
#     prunables = torch.tensor(prunables)

#     # linear mapping
#     max = torch.max(scores)
#     min = torch.min(scores)
    
#     layerwise_pruning_ratios = (((scores - min) / (max - min)) * (s2 - s1) + s1)
#     scaler = torch.sum(prunables) * args.sparsity_ratio / (torch.sum(prunables * layerwise_pruning_ratios))  
#     layerwise_pruning_ratios = layerwise_pruning_ratios * scaler
#     layerwise_pruning_ratios = layerwise_pruning_ratios.cpu().numpy().tolist()
    
#     print(layerwise_pruning_ratios)
#     return layerwise_pruning_ratios

# #########################################################################################################################

# def prune_magnitude(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0, ratios=None):
#     if "OPT" in model.__class__.__name__:
#         layers = model.model.decoder.layers
#     else:    
#         layers = model.model.layers
    
#     layer_num = len(find_layers(layers))
#     if ratios is None:
#         ratios = [args.sparsity_ratio for i in range(layer_num)]

#     k=0
#     for i in range(len(layers)):
#         layer = layers[i]
#         subset = find_layers(layer)

#         for name in subset:
#             W = subset[name].weight.data 
#             W_metric = torch.abs(W)
#             print(ratios[k])
#             thresh = torch.sort(W_metric.flatten().cuda())[0][int(W.numel()*ratios[k])].cpu()
#             W_mask = (W_metric<=thresh)
#             k+=1

#             W[W_mask] = 0
    

# def prune_wanda(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0, ratios=None):
#     use_cache = model.config.use_cache 
#     model.config.use_cache = False 

#     print("loading calibdation data")
#     dataloader, _ = get_loaders("c4",nsamples=args.nsamples,seed=args.seed,seqlen=model.seqlen,tokenizer=tokenizer)
#     print("dataset loading complete")
#     with torch.no_grad():
#         if "OPT" in model.__class__.__name__:
#             inps, outs, attention_mask, position_ids = prepare_calibration_input_opt(model, dataloader, device)
#         else:
#             inps, outs, attention_mask, position_ids = prepare_calibration_input(model, dataloader, device)


#     print ("inps",inps)

#     if "OPT" in model.__class__.__name__:
#         layers = model.model.decoder.layers
#     else:    
#         layers = model.model.layers
    
#     layer_num = len(find_layers(layers))
#     if ratios is None:
#         ratios = [args.sparsity_ratio for i in range(layer_num)]
    
#     k=0
#     for i in range(len(layers)):
#         layer = layers[i]
#         subset = find_layers(layer)

#         if f"model.layers.{i}" in model.hf_device_map:   ## handle the case for llama-30B and llama-65B, when the device map has multiple GPUs;
#             dev = model.hf_device_map[f"model.layers.{i}"]
#             inps, outs, attention_mask, position_ids = inps.to(dev), outs.to(dev), attention_mask.to(dev), position_ids.to(dev)

#         wrapped_layers = {}
#         for name in subset:
#             wrapped_layers[name] = WrappedGPT(subset[name])

#         def add_batch(name):
#             def tmp(_, inp, out):
#                 wrapped_layers[name].add_batch(inp[0].data, out.data)
#             return tmp

#         handles = []
#         for name in wrapped_layers:
#             handles.append(subset[name].register_forward_hook(add_batch(name)))
#         for j in range(args.nsamples):
#             with torch.no_grad():
#                 if "OPT" in model.__class__.__name__:
#                     outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
#                 else:
#                     outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]
#         for h in handles:
#             h.remove()

#         for name in subset:
#             print(f"pruning layer {i} name {name}")
#             W_metric = torch.abs(subset[name].weight.data) * torch.sqrt(wrapped_layers[name].scaler_row.reshape((1,-1)))

#             W_mask = (torch.zeros_like(W_metric) == 1)  ## initialize a mask to be all False
#             if prune_n != 0:
#                 # structured n:m sparsity
#                 for ii in range(W_metric.shape[1]):
#                     if ii % prune_m == 0:
#                         tmp = W_metric[:,ii:(ii+prune_m)].float()
#                         W_mask.scatter_(1,ii+torch.topk(tmp, prune_n,dim=1, largest=False)[1], True)
#             else:
#                 sort_res = torch.sort(W_metric, dim=-1, stable=True)

#                 if args.use_variant:
#                     # wanda variant 
#                     tmp_metric = torch.cumsum(sort_res[0], dim=1)
#                     sum_before = W_metric.sum(dim=1)

#                     alpha = 0.4
#                     alpha_hist = [0., 0.8]
#                     W_mask, cur_sparsity = return_given_alpha(alpha, sort_res, W_metric, tmp_metric, sum_before)
#                     while (torch.abs(cur_sparsity - args.sparsity_ratio)>0.001) and (alpha_hist[1]-alpha_hist[0]>=0.001):
#                         if cur_sparsity > args.sparsity_ratio:
#                             alpha_new = (alpha + alpha_hist[0]) / 2.0
#                             alpha_hist[1] = alpha
#                         else:
#                             alpha_new = (alpha + alpha_hist[1]) / 2.0
#                             alpha_hist[0] = alpha

#                         alpha = alpha_new 
#                         W_mask, cur_sparsity = return_given_alpha(alpha, sort_res, W_metric, tmp_metric, sum_before)
#                     print(f"alpha found {alpha} sparsity {cur_sparsity:.6f}")
#                 else:
#                     # unstructured pruning
#                     indices = sort_res[1][:,:int(W_metric.shape[1]*ratios[k])]
#                     k+=1
#                     W_mask.scatter_(1, indices, True)
# #             print ("W_mask",W_mask)
#             subset[name].weight.data[W_mask] = 0  ## set weights to zero 

#         for j in range(args.nsamples):
#             with torch.no_grad():
#                 if "OPT" in model.__class__.__name__:
#                     outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
#                 else:
#                     outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]
#         inps, outs = outs, inps

#     model.config.use_cache = use_cache 
#     torch.cuda.empty_cache()


# @torch.no_grad()
# def prune_sparsegpt(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0, ratios=None):
#     ## SparseGPT code available at: https://github.com/IST-DASLab/sparsegpt/tree/f5c25005a61f96a0933ca2f95705a963585aafaa
#     print('Starting ...')
#     use_cache = model.config.use_cache
#     model.config.use_cache = False
    
#     dataloader, _ = get_loaders("c4",nsamples=args.nsamples,seed=args.seed,seqlen=model.seqlen,tokenizer=tokenizer)
    
#     with torch.no_grad():
#         if "OPT" in model.__class__.__name__:
#             inps, outs, attention_mask, position_ids = prepare_calibration_input_opt(model, dataloader, device)
#         else:
#             inps, outs, attention_mask, position_ids = prepare_calibration_input(model, dataloader, device)

#     if "OPT" in model.__class__.__name__:
#         layers = model.model.decoder.layers
#     else:    
#         layers = model.model.layers        
    
    
#     layer_num = len(find_layers(layers))
#     if ratios is None:
#         ratios = [args.sparsity_ratio for i in range(layer_num)]
    
#     print('Ready.')
#     k=0
#     for i in range(len(layers)):
#         layer = layers[i]
#         if f"model.layers.{i}" in model.hf_device_map:
#             print("using !")
#             dev = model.hf_device_map[f"model.layers.{i}"]
#             print(f"layer {i} device {dev}")
#             inps, outs, attention_mask, position_ids = inps.to(dev), outs.to(dev), attention_mask.to(dev), position_ids.to(dev)

#         subset = find_layers(layer)

#         gpts = {}
#         for name in subset:
#             gpts[name] = SparseGPT(subset[name])

#         def add_batch(name):
#             def tmp(_, inp, out):
#                 gpts[name].add_batch(inp[0].data, out.data)
#             return tmp

#         handles = []
#         for name in gpts:
#             handles.append(subset[name].register_forward_hook(add_batch(name)))

#         for j in range(args.nsamples):
#             if "OPT" in model.__class__.__name__:
#                 outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
#             else:
#                 outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]
#         for h in handles:
#             h.remove()

#         for name in gpts:
#             print(i, name)
#             print('Pruning ...')

#             gpts[name].fasterprune(ratios[k], prune_n=prune_n, prune_m=prune_m, percdamp=0.01, blocksize=128)
#             gpts[name].free()
#             k+=1

#         for j in range(args.nsamples):
#             if "OPT" in model.__class__.__name__:
#                 outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
#             else:
#                 outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]

#         layers[i] = layer 
#         torch.cuda.empty_cache()

#         inps, outs = outs, inps

#     model.config.use_cache = use_cache
#     torch.cuda.empty_cache()

# ###############################################################################################################
# def prune_magnitude_ww(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0):
#     s1 = 1.0 - args.epsilon
#     s2 = 1.0 + args.epsilon
    
#     all_layer_ratio = ww_sparsity(args, model, device, s1, s2)
#     # magnitude pruning
#     prune_magnitude(args, model, tokenizer, device, ratios=all_layer_ratio)
    

# def prune_wanda_ww(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0):
#     s1 = 1.0 - args.epsilon
#     s2 = 1.0 + args.epsilon

#     all_layer_ratio = ww_sparsity(args, model, device, s1, s2)
#     # wanda pruning
#     prune_wanda(args, model, tokenizer, device, ratios=all_layer_ratio)
    
# def prune_sparsegpt_ww(args, model, tokenizer, device=torch.device("cuda:0"), prune_n=0, prune_m=0):
#     s1 = 1.0 - args.epsilon
#     s2 = 1.0 + args.epsilon

#     all_layer_ratio = ww_sparsity(args, model, device, s1, s2)
#     # sparsegpt pruning
#     prune_sparsegpt(args, model, tokenizer, device, ratios=all_layer_ratio)
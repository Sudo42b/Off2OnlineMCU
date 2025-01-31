import torch.quantization as quantization
import torch
from torch import nn as nn
import numpy as np
import sys
import os
from tqdm import tqdm
from torch.autograd import Variable
from subprocess import call
from .fully_connected_opt_weight_generation import convert_to_x4_q7_weights

class CMSISConverter:
    def __init__(self, root, model, weight_file_name, parameter_file_name,
                linear_features:str, 
                weight_bits:int=8,
                compilation_config=None):
        # TODO: defined by user should be
        self.root = root
        self.io_folder = os.path.join(self.root, "logs")
        os.makedirs(self.io_folder, exist_ok=True)
        self.model = model
        self.parameter_file_name =  os.path.join(self.root, parameter_file_name)
        self.weight_file_name =  os.path.join(self.root, weight_file_name)
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu"
        )
        with open(self.parameter_file_name, "w") as w_file:
            w_file.write("#pragma once\n")
        with open(self.weight_file_name, "w") as w_file:
            w_file.write("#pragma once\n")

        self.compilation = compilation_config
        # define storage for maximum buffers in CMSIS
        self.max_col_buffer = 0
        self.max_fc_buffer = 0

        self.weight_bits = weight_bits
        # TODO: the image shape befor efully connected is only known because
        # there is a function the model that gets it. Should be independent of tht function
        self.compilation_config = compilation_config
        self.conv_linear_interface_shape = linear_features
        

        # here we suppose an 8bit signed number in original range [0, 1]
        # which corresponds to range 7 fractional bits
        self.input_frac_bits = 7 
        self.fractional_bits = {}

        # for storing all convolution, pooling and linear params
        self.params = {}
        self.param_prefix_name = None

        # storing inputs and ouputs quantized
        self.logging = {}

    def quantize_input(self, input):
        self.input_frac_bits = self.compute_fractional_bits_tensor(input)
        if input.is_cuda:
            input = input.cpu()
        qtensor = self.quantize_tensor(input).numpy().astype(np.int8)
        # save to qtensor csv files.
        #CHW -> HWC 
        qtensor = qtensor.transpose(1, 2, 0)
        qtensor.tofile(os.path.join(self.io_folder, 'input.raw'))

    def compute_fractional_bits_tensor(self, weight):
        return self.weight_bits - 1 - compute_fractional_bits(torch.min(weight), torch.max(weight))

    def quantize_tensor(self, weight):
        q_frac = self.compute_fractional_bits_tensor(weight)
        return torch.ceil(weight*(2**q_frac)).type(torch.int8)

    def generate_intermediate_values(self, loader):
        register_hooks(self.model)
        from tqdm import tqdm
        for sample, _ in tqdm(loader, total=len(loader)):
            if not sample.is_cuda:
                sample = sample.cuda()
            _ = self.model(sample)
    
    def convert_model(self, val_loader):
        self.generate_intermediate_values(val_loader)
        self.convert_model_cmsis()
        
    def convert_model_cmsis(self):
        count_conv = 1
        count_linear = 1
        count_pool = 1

        for module in self.model.modules():
            if isinstance(module, nn.Conv2d):
                self.param_prefix_name = "CONV" + str(count_conv)
                self.convert_module(module)
                self.save_params_conv(module)
                count_conv += 1

            if isinstance(module, nn.Linear):
                self.param_prefix_name = "FC" + str(count_linear)
                self.convert_module(module)
                self.save_params_linear(module)
                count_linear += 1

            if isinstance(module, (nn.MaxPool2d, nn.AvgPool2d, nn.AdaptiveAvgPool2d)):
                self.param_prefix_name = "POOL" + str(count_pool)
                self.save_params_pool(module)
                count_pool += 1

        self.write_shifts_n_params()

    def convert_module(self, module:nn.Module):
        act_bits = self.weight_bits - 1 - compute_fractional_bits(module.output_min_val, module.output_max_val)
        inp_bits = self.weight_bits - 1 - compute_fractional_bits(module.input_min_val, module.input_max_val)
        # suposes that module has two named parameters: weight and bias
        self.compute_output_bias_shifts(module.weight, module.bias, act_bits, inp_bits)
        for name, tensor in module.named_parameters():
            self.convert_conv_linear_weight_cmsis(name, tensor)

    def compute_output_bias_shifts(self, weight, bias, activation_bits, input_bits):
        q_weight = self.compute_fractional_bits_tensor(weight)
        q_bias = self.compute_fractional_bits_tensor(bias)

        self.fractional_bits[self.param_prefix_name + "_BIAS_LSHIFT"] = input_bits + q_weight - q_bias
        self.fractional_bits[self.param_prefix_name + "_OUT_RSHIFT"] = input_bits + q_weight - activation_bits
        self.fractional_bits[self.param_prefix_name + "_Q"] = q_weight
        self.fractional_bits[self.param_prefix_name + "_BIAS_Q"] = q_bias
        self.fractional_bits[self.param_prefix_name + "_INPUT_Q"] = input_bits
        self.fractional_bits[self.param_prefix_name + "_OUT_Q"] = activation_bits

    def convert_conv_linear_weight_cmsis(self, tensor_name, weight):
        if tensor_name == 'bias':
            name = self.param_prefix_name + "_BIAS"
        if tensor_name == "weight":
            name = self.param_prefix_name + "_WT"

        qweight = self.quantize_tensor(weight)
        if qweight.is_cuda:
            qweight = qweight.cpu()
        if tensor_name == 'bias':
            self.write_weights(name, qweight.numpy().astype(np.int8))
        if tensor_name == "weight":
            if "CONV" in name:
                # torch has conv weighs (out, in, h, w) while cmsis
                # (o, h, w, i). like in tutorial for legacy
                self.write_weights(name, qweight.permute(0, 2, 3, 1).numpy().astype(np.int8))
            elif "FC" in name:
                original_shape = qweight.shape
                if "FC1" == name.split("_")[0]:
                    print(name)
                    print(qweight.shape)
                    print(self.conv_linear_interface_shape)
                    trans_weight = qweight.reshape(original_shape[0], 
                                                   *tuple(torch.tensor(self.conv_linear_interface_shape).numpy().tolist()))\
                                            .permute(0, 2, 3, 1)\
                                            .reshape(original_shape)
                    weight = convert_to_x4_q7_weights(trans_weight.reshape(original_shape[0], original_shape[1], 1, 1).numpy().astype(np.int8))
                else:
                    weight = convert_to_x4_q7_weights(qweight.reshape(original_shape[0], original_shape[1], 1, 1).numpy().astype(np.int8))
                self.write_weights(name, weight)
                        
    def save_params_conv(self, module):
        self.params[self.param_prefix_name + "_IM_CH"] = module.in_channels
        self.params[self.param_prefix_name + "_OUT_CH"] = module.out_channels

        # kernel has to be squared
        if isinstance(module.kernel_size, tuple):
            kernel = module.kernel_size[0]
        else:
            kernel = module.kernel_size
        self.params[self.param_prefix_name + "_KER_DIM"] = kernel

        if isinstance(module.padding, tuple):
            padding = module.padding[0]
        else:
            padding = module.padding
        self.params[self.param_prefix_name + "_PADDING"] = padding

        if isinstance(module.stride, tuple):
            stride = module.stride[0]
        else:
            stride = module.stride
        self.params[self.param_prefix_name + "_STRIDE"] = stride
        
        self.params[self.param_prefix_name + "_IM_DIM"] = module.input_shape[-1]
        self.params[self.param_prefix_name + "_OUT_DIM"] = module.output_shape[-1]

        col_buffer = 2*module.in_channels*kernel*kernel
        if self.max_col_buffer < col_buffer:
            self.max_col_buffer = col_buffer
            self.params["MAX_CONV_BUFFER_SIZE"] = self.max_col_buffer
        
    def save_params_pool(self, module):
        self.params[self.param_prefix_name + "_OUT_CH"] = module.output_shape[0]
        self.params[self.param_prefix_name + "_IM_CH"] = module.input_shape[1]
        
        # kernel has to be squared
        if isinstance(module.kernel_size, tuple):
            kernel = module.kernel_size[0]
        else:
            kernel = module.kernel_size
        self.params[self.param_prefix_name + "_KER_DIM"] = kernel

        if isinstance(module.padding, tuple):
            padding = module.padding[0]
        else:
            padding = module.padding
        self.params[self.param_prefix_name + "_PADDING"] = padding

        if isinstance(module.stride, tuple):
            stride = module.stride[0]
        else:
            stride = module.stride
        self.params[self.param_prefix_name + "_STRIDE"] = stride
        
        self.params[self.param_prefix_name + "_IM_DIM"] = module.input_shape[-1]
        self.params[self.param_prefix_name + "_OUT_DIM"] = module.output_shape[-1]
        
    def save_params_linear(self, module):
        self.params[self.param_prefix_name + "_OUT"] = module.out_features
        self.params[self.param_prefix_name + "_DIM"] = torch.prod(
            torch.tensor(
                module.input_shape[-1:])).item()

        if self.max_fc_buffer < self.params[self.param_prefix_name + "_DIM"]:
            self.max_fc_buffer = self.params[self.param_prefix_name + "_DIM"]
            self.params["MAX_FC_BUFFER"] = self.max_fc_buffer

    def write_shifts_n_params(self):
        with open(self.parameter_file_name, "w+") as w_file:
            for i, j in self.fractional_bits.items():
                w_file.write("#define " + i + " " + str(j) + "\n")
            for i, j in self.params.items():
                w_file.write("#define " + i + " " + str(j) + "\n")

    def write_logging(self):
        for i, j in self.logging.items():
            j.tofile(os.path.join(self.io_folder, str(i).lower() + "_torch.raw"))
    
    def write_weights(self, name, weight):
        with open(self.weight_file_name, "a") as w_file:
            w_file.write("#define " + name + " {")
            weight.tofile(w_file, sep=',')
            w_file.write("}\n")
            w_file.write("#define " + name + "_SHAPE ")
            w_file.write(str(np.prod(weight.shape)))
            w_file.write("\n")

    def register_logging(self):
        count_conv = 1
        count_linear = 1
        count_pool = 1
        self.model = self.model.cpu()

        for module in self.model.modules():
            if isinstance(module, nn.Conv2d):
                self.param_prefix_name = "CONV" + str(count_conv)
                self.logging[self.param_prefix_name + "_OUT"] = \
                    self.quantize_tensor(module.output).cpu().numpy()
                count_conv += 1
            if isinstance(module, nn.Linear):
                self.param_prefix_name = "FC" + str(count_linear)
                self.logging[self.param_prefix_name + "_OUT"] = \
                    self.quantize_tensor(module.output).cpu().numpy()
                count_linear += 1
            if isinstance(module, nn.MaxPool2d):
                self.param_prefix_name = "POOL" + str(count_pool)
                self.logging[self.param_prefix_name + "_OUT"] = \
                    self.quantize_tensor(module.output).cpu().numpy()
                count_pool += 1

        self.write_logging()



    def compile(self):
        call(self.compilation, cwd=self.root, shell=True)

    def execute(self, exec_path):
        call(os.path.join("./", exec_path), cwd=self.root)
        #TODO: this implies that the executable produces this file
        return np.fromfile(os.path.join(self.io_folder, "y_out.raw"), dtype=np.int8)

    def evaluate_cmsis(self, exec_path, loader):
        correct = 0
        total = 0
        self.compile()
        for input_batch, label_batch in tqdm(loader, total=len(loader)):
            for input, label in zip(input_batch, label_batch):
                self.quantize_input(input)
                out = self.execute(exec_path)
                pred = np.argmax(out)
                correct += (pred == label.item())
                total += 1
                break
        print("Test accuracy for CMSIS model {}".format(correct/total))

    def sample_inference_checker(self, exec_path, input):
        self.compile()
        self.quantize_input(input[0])
        out = self.execute(exec_path)
        
        self.model = self.model.to(self.device)
        input = input.to(self.device)
        out_torch = self.model(input)[0]
        # print(out, out_torch)
        self.register_logging()


def hook_save_params(module, input, output):
    setattr(module, "input_shape", input[0].shape)
    setattr(module, "output_shape", output[0].shape)
    setattr(module, "input", input[0][0])
    setattr(module, "output", output[0])
    if not module.output_max_val.is_cuda:
        module.output_max_val = module.output_max_val.cuda()
        module.output_min_val = module.output_min_val.cuda()
    if not module.input_max_val.is_cuda:
        module.input_max_val = module.input_max_val.cuda()
        module.input_min_val = module.input_min_val.cuda()
        
    if module.output_max_val < torch.max(output):
        module.output_max_val = torch.max(output)
    if module.output_min_val > torch.min(output):
        module.output_min_val = torch.min(output)
    if module.input_max_val < torch.max(input[0]):
        module.input_max_val = torch.max(input[0])
    if module.input_min_val > torch.min(input[0]):
        module.input_min_val = torch.min(input[0])

def register_hooks(model):
    for module in model.modules():
        if isinstance(module, (nn.Conv2d, nn.Linear, nn.MaxPool2d, nn.AvgPool2d, nn.AdaptiveAvgPool2d)):
            module.register_buffer("input_min_val", torch.tensor(float('inf')))
            module.register_buffer("input_max_val", torch.tensor(float('-inf')))
            module.register_buffer("output_max_val", torch.tensor(float('-inf')))
            module.register_buffer("output_min_val", torch.tensor(float('inf')))
            module.register_forward_hook(hook_save_params)

def compute_fractional_bits(min_value, max_value):
    return int(torch.ceil(torch.log2(torch.max(torch.abs(max_value), torch.abs(min_value)))).item())
import argparse
import torch
import torch.nn as nn
import numpy as np
from colossalai.utils import get_current_device
from colossalai.gemini.memory_tracer import SyncCudaMemoryMonitor
from colossalai.tensor.param_op_hook import ParamOpHook
from colossalai.tensor.param_op_hook import ParamOpHookManager
from colossalai.utils.model.colo_init_context import ColoInitContext
from colossalai.nn.parallel.data_parallel import _cast_float
from contextlib import contextmanager
from enum import Enum
from functools import partial
from typing import List

from model_utils import *


class TrainingPhase(Enum):
    FORWARD = 0
    BACKWARD = 1


class MemInfo():
    model_data_list = []
    non_model_data_list = []
    unreleased_grad_flag = {}
    temp_grad_volume = 0



class MyParamHook(ParamOpHook):

    def __init__(self, model_data_mem: int=0) -> None:
        super().__init__()
        self.model_data_mem = model_data_mem
        self._training_phase = TrainingPhase.FORWARD
        self.mem_monitor = SyncCudaMemoryMonitor()

    def sample_model_data(self, params):
        if self._training_phase == TrainingPhase.FORWARD:
            MemInfo.model_data_list.append(self.model_data_mem)
        elif self._training_phase == TrainingPhase.BACKWARD:
            data_volume = self.model_data_mem
            for p in params:
                cur_param_data_volume = p.data.numel() * p.data.element_size()
                if p.requires_grad:
                    # add param.grad, actually param.grad is None in this time
                    data_volume += cur_param_data_volume
                    if not MemInfo.unreleased_grad_flag[p]:
                        self.model_data_mem += cur_param_data_volume
                        MemInfo.unreleased_grad_flag[p] = True
                    else:
                        MemInfo.temp_grad_volume += cur_param_data_volume
            MemInfo.model_data_list.append(data_volume)


    def pre_op(self, params):
        cuda_volume = self.mem_monitor.finish()
        if len(MemInfo.model_data_list):
            if self._training_phase == TrainingPhase.BACKWARD and MemInfo.temp_grad_volume > 0:
                max_non_model_data = max(MemInfo.non_model_data_list[-1],
                                         cuda_volume - MemInfo.temp_grad_volume - MemInfo.model_data_list[-1],
                                         torch.cuda.memory_allocated() - self.model_data_mem)
                # print("pre", MemInfo.non_model_data_list[-1] / 1024 ** 2,
                #       (cuda_volume - MemInfo.temp_grad_volume - MemInfo.model_data_list[-1]) / 1024 ** 2,
                #       (torch.cuda.memory_allocated() - self.model_data_mem) / 1024 ** 2)
                MemInfo.non_model_data_list[-1] = max_non_model_data
                MemInfo.temp_grad_volume = 0
            else:
                MemInfo.non_model_data_list.append(cuda_volume - MemInfo.model_data_list[-1])
        self.sample_model_data(params)
        self.mem_monitor.start()

    def post_op(self, params):
        if self._training_phase == TrainingPhase.BACKWARD and MemInfo.temp_grad_volume > 0:
            cuda_volume = self.mem_monitor.finish()
            MemInfo.non_model_data_list.append(cuda_volume - MemInfo.model_data_list[-1])
            # print("post", cuda_volume/1024**2, (cuda_volume - MemInfo.model_data_list[-1])/1024**2)
            self.mem_monitor.start()



    def pre_forward(self, params: List[torch.Tensor]) -> None:
        self.pre_op(params)

    def post_forward(self, params: List[torch.Tensor]) -> None:
        self.post_op(params)

    def pre_backward(self, params: List[torch.Tensor]) -> None:
        self.pre_op(params)

    def post_backward(self, params: List[torch.Tensor]) -> None:
        self.post_op(params)

    @contextmanager
    def switch_training_phase(self, training_phase: TrainingPhase = TrainingPhase.BACKWARD):
        old_training_phase = self._training_phase
        try:
            self._training_phase = training_phase
            yield
        finally:
            self._training_phase = old_training_phase

    switch_to_backward = switch_training_phase
    switch_to_forward = partial(switch_to_backward, training_phase=TrainingPhase.FORWARD)


class MyParamWrapper():

    def __init__(self, module: torch.nn.Module, dtype: torch.dtype=torch.float, model_mem: int=0):
        super().__init__()
        self.module = module
        self.dtype = dtype
        self.model_mem = model_mem
        self.param_op_hook = MyParamHook(model_data_mem=model_mem)

        for p in module.parameters():
            p.data = p.data.to(dtype)

        self._cast_buffers_to_cuda_dtype()


    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

    def _pre_forward(self):
        self._clear_cuda_mem_info()
        for p in self.module.parameters():
            if p.requires_grad:
                MemInfo.unreleased_grad_flag[p] = False
        self.param_op_hook.mem_monitor.start()

    def forward(self, *args, **kwargs):
        args, kwargs = _cast_float(args, self.dtype), _cast_float(kwargs, self.dtype)
        self.module.zero_grad(set_to_none=True)
        self._pre_forward()
        with ParamOpHookManager.use_hooks(self.param_op_hook):
            outputs = self.module(*args, **kwargs)
        return outputs

    def backward(self, loss):
        with self.param_op_hook.switch_to_backward(), ParamOpHookManager.use_hooks(self.param_op_hook):
            loss.backward()
        self._post_backward()

    def _post_backward(self):
        cuda_volume = self.param_op_hook.mem_monitor.finish()
        last_model_data = MemInfo.model_data_list[-1]

        if MemInfo.temp_grad_volume > 0:
            max_non_model_data = max(MemInfo.non_model_data_list[-1],
                                     cuda_volume - MemInfo.temp_grad_volume - MemInfo.model_data_list[-1],
                                     torch.cuda.memory_allocated() - self.param_op_hook.model_data_mem)
            MemInfo.non_model_data_list[-1] = max_non_model_data
            MemInfo.temp_grad_volume = 0
        else:
            MemInfo.non_model_data_list.append(cuda_volume - last_model_data)

    def _clear_cuda_mem_info(self):
        MemInfo.model_data_list.clear()
        MemInfo.non_model_data_list.clear()
        MemInfo.unreleased_grad_flag.clear()
        MemInfo.temp_grad_volume = 0

    def _cast_buffers_to_cuda_dtype(self):
        for buffer in self.module.buffers():
            buffer.data = buffer.cuda()
            if torch.is_floating_point(buffer):
                buffer.data = buffer.data.to(self.dtype)



def run_param_wrapper_testing(model_name="", iter_num=1):

    get_components_func = non_distributed_component_funcs.get_callable(model_name)
    model_builder, data_gen = get_components_func()

    with ColoInitContext(device=torch.device('cuda')):
        model = model_builder(checkpoint=True)
    mem_model_data = torch.cuda.memory_allocated()

    data_args = data_gen(device=get_current_device())

    model = MyParamWrapper(model, dtype=torch.float, model_mem=mem_model_data)

    print("model data", torch.cuda.memory_allocated() / 1024**2)

    for iter in range(iter_num):
        output = model(**data_args)
        loss = torch.mean(output)
        model.backward(loss)

    cuda_non_model_data_list = np.array(MemInfo.non_model_data_list) / 1024 ** 2
    print("cuda_non_model_data_list", len(cuda_non_model_data_list))
    print(MemInfo.non_model_data_list)

    res_file = open("tracer_results/verify_v3_" + model_name + ".txt", "w", encoding="utf-8")
    for ddd in cuda_non_model_data_list:
        res_file.write(str(ddd) + "\n")
    res_file.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Wrapper Tracer")
    parser.add_argument("-m_name", type=str, default="simplenet",
                        help="model name")
    parser.add_argument("-iter_num", type=int, default=1, help="Number of iterations")
    args = parser.parse_args()
    run_param_wrapper_testing(args.m_name, args.iter_num)

import torch
import torch.nn as nn
from colossalai.gemini.memory_tracer import SyncCudaMemoryMonitor
from colossalai.gemini.chunk.chunk import free_storage, alloc_storage
from colossalai.tensor.param_op_hook import ParamOpHook
from colossalai.tensor.param_op_hook import ParamOpHookManager
from colossalai.utils.model.colo_init_context import ColoInitContext
from contextlib import contextmanager
from enum import Enum
from functools import partial
from typing import List


class TrainingPhase(Enum):
    FORWARD = 0
    BACKWARD = 1


class MyParamHook(ParamOpHook):

    def __init__(self, dtype: torch.dtype=torch.float) -> None:
        super().__init__()
        self._training_phase = TrainingPhase.FORWARD
        self.dtype = dtype

    def _free_cuda_params(self, params):
        for p in params:
            if p.data.device.type == "cpu":
                raise NotImplementedError("Only free cuda memory")
            p.cpu_data = torch.empty(p.data.shape, dtype=self.dtype, device="cpu")
            p.cpu_data.copy_(p.data)
            free_storage(p.data)

    def _allocate_params_on_cuda(self, params):
        for p in params:
            cur_dev = p.data.device.type
            if cur_dev == "cpu":
                if p.grad is not None and p.grad.device.type == "cpu":
                    raise NotImplementedError("Only run in forward propagation")
                p.cpu_data = p.data
                p.data = torch.empty(p.data.shape, device="cuda", dtype=self.dtype, requires_grad=p.data.requires_grad)
                p.data.copy_(p.cpu_data)
            elif cur_dev == "cuda":
                alloc_storage(p.data)
                p.data.copy_(p.cpu_data)
            free_storage(p.cpu_data)

    def _move_params_to_dev(self, params, dev: str) -> int:
        assert isinstance(dev, str), f"device should be a str not torch.device"
        comm_volume = 0

        for p in params:
            p.temp_data = p.data
            p.data = torch.empty(p.data.shape, device=dev, dtype=p.data.dtype,
                        requires_grad=p.data.requires_grad)
            p.data.copy_(p.temp_data)
            free_storage(p.temp_data)
            del p.temp_data

        return comm_volume


    def pre_op(self, params):
        if self._training_phase == TrainingPhase.BACKWARD:
            print("pre_op", torch.cuda.memory_allocated()/1024**2, torch.cuda.max_memory_allocated()/1024**2)
            torch.cuda.reset_peak_memory_stats()

        self._allocate_params_on_cuda(params)
        # self._move_params_to_dev(params, "cuda")

    def post_op(self, params):
        if self._training_phase == TrainingPhase.BACKWARD:
            print("post_op", torch.cuda.memory_allocated()/1024**2, torch.cuda.max_memory_allocated()/1024**2)

        self._free_cuda_params(params)
        # self._move_params_to_dev(params, "cpu")

        # if self._training_phase == TrainingPhase.BACKWARD:
        #     print("post op", torch.cuda.memory_allocated()/1024**2)
        # torch.cuda.reset_peak_memory_stats()

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

    def __init__(self, module: torch.nn.Module):
        super().__init__()
        self.module = module
        self.param_op_hook = MyParamHook()
        for p in module.parameters():
            if getattr(p, '_ddp_to_ignore', False):
                continue
            if p.requires_grad:
                p.register_hook(partial(self.grad_handle, p))

    def grad_handle(self, p, grad):
        pass
        # print("bef move grad", torch.cuda.memory_allocated()/1024**2)
        # if p.grad is not None:
        #     print("moving")
        #     p.grad = p.grad.to("cpu")
        # print("aft move grad", torch.cuda.memory_allocated()/1024**2)

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

    def _pre_forward(self):
        pass

    def forward(self, *args, **kwargs):
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
        pass



class MyModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(1024, 1024, bias=False)
        self.fc2 = nn.Linear(1024, 1024, bias=False)

    def forward(self, x):
        out = self.fc1(x)
        print("aft fc1", torch.cuda.memory_allocated()/1024**2)
        out = self.fc2(out)
        print("aft fc2", torch.cuda.memory_allocated()/1024**2)
        out = self.fc2(out)
        print("aft fc2", torch.cuda.memory_allocated() / 1024**2)
        out = self.fc2(out)
        print("aft fc2", torch.cuda.memory_allocated() / 1024 ** 2)
        # print(sys.getrefcount(self.fc1), sys.getrefcount(self.fc1.weight), sys.getrefcount(self.fc1.weight.data),
        #       sys.getrefcount(self.fc1.weight.data.storage()))
        return out


data = torch.randn((1, 1024)).cuda()
with ColoInitContext(device=torch.device("cpu")):
    net = MyModel()
net = MyParamWrapper(net)
out = net(data)
loss = torch.mean(out)
net.backward(loss)

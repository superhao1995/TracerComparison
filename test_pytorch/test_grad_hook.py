
import torch
import torch.nn as nn
from colossalai.tensor.param_op_hook import ParamOpHook
from colossalai.tensor.param_op_hook import ParamOpHookManager
from colossalai.utils.model.colo_init_context import ColoInitContext
from contextlib import contextmanager
from enum import Enum
from functools import partial
from typing import List

def is_storage_empty(tensor: torch.Tensor) -> bool:
    return tensor.storage().size() == 0

def free_storage(tensor: torch.Tensor) -> None:
    if not is_storage_empty(tensor):
        tensor.storage().resize_(0)

class TrainingPhase(Enum):
    FORWARD = 0
    BACKWARD = 1


class MyParamHook(ParamOpHook):

    def __init__(self) -> None:
        super().__init__()
        self._training_phase = TrainingPhase.FORWARD

    def _move_params_to_dev(self, params, dev: str) -> int:

        assert isinstance(dev, str), f"device should be a str not torch.device"
        comm_volume = 0
        for p in params:

            if dev == "cuda":
                if p.data.device.type == "cpu":
                    # p.data = p.data.to(dev)
                    p.data = torch.randn(p.data.shape, device="cuda")
                elif p.data.device.type == "cuda":
                    p.data.storage().resize_(p.data.numel())
            elif dev == "cpu":
                free_storage(p.data)

        return comm_volume

    def pre_op(self, params):
        if self._training_phase == TrainingPhase.BACKWARD:
            print("pre_op", torch.cuda.memory_allocated()/1024**2, torch.cuda.max_memory_allocated()/1024**2)
            torch.cuda.reset_peak_memory_stats()
        self._move_params_to_dev(params, 'cuda')

    def post_op(self, params):
        if self._training_phase == TrainingPhase.BACKWARD:
            print("post_op", torch.cuda.memory_allocated()/1024**2, torch.cuda.max_memory_allocated()/1024**2)
        self._move_params_to_dev(params, 'cpu')

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
        print("bef free grad", torch.cuda.memory_allocated()/1024**2)
        if grad is not None:
            print("free grad")
            free_storage(grad)
        print("aft free grad", torch.cuda.memory_allocated()/1024**2)

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
        # self.fc3 = nn.Linear(1024, 1024, bias=False)

    def forward(self, x):
        out = self.fc1(x)
        print("aft fc1", torch.cuda.memory_allocated()/1024**2)
        out = self.fc2(out)
        print("aft fc2", torch.cuda.memory_allocated()/1024**2)
        out = self.fc2(out)
        print("aft fc2", torch.cuda.memory_allocated() / 1024**2)
        out = self.fc2(out)
        print("aft fc2", torch.cuda.memory_allocated() / 1024 ** 2)
        return out


data = torch.randn((1, 1024)).cuda()
with ColoInitContext(device=torch.device("cpu")):
    net = MyModel()
net = MyParamWrapper(net)
out = net(data)
loss = torch.mean(out)
net.backward(loss)

import torch.nn

from paramOpHook import ParamHook, MemInfo, GradHook
from colossalai.tensor.param_op_hook import ParamOpHookManager
from colossalai.nn.parallel.data_parallel import _cast_float


class ParamWrapper():

    def __init__(self, module: torch.nn.Module, dtype: torch.dtype = torch.half):
        super().__init__()
        self.module = module
        self.dtype = dtype
        self.param_op_hook = ParamHook()
        self.grad_hook = GradHook(module)
        self.cpu_param_data_dict = {}

        for p in module.parameters():
            # assert isinstance(p, ColoParameter)
            # print(type(p), p.data.shape)
            p.data = p.data.to(dtype)

        self._cast_buffers_to_cuda_dtype()

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

    def _save_param_data_on_cpu(self):
        for p in self.module.parameters():
            # print("save", type(p.data), p.data.shape, p.requires_grad)
            # self.cpu_param_data_dict[p] = torch.empty(p.data.shape, dtype=self.dtype, device="cpu",
            #                                           requires_grad=p.data.requires_grad)
            self.cpu_param_data_dict[p] = torch.empty(p.data.shape, dtype=self.dtype, device="cpu")
            self.cpu_param_data_dict[p].copy_(p.data)

    def _restore_param_data(self):
        for p in self.module.parameters():
            # print("restore", type(p), type(p.data), p.data.shape)
            p.data = torch.empty(p.data.shape, dtype=self.dtype, device="cpu", requires_grad=p.data.requires_grad)
            p.data.copy_(self.cpu_param_data_dict[p])
        self.cpu_param_data_dict.clear()

    def _pre_forward(self):
        self._clear_mem_info()
        self._save_param_data_on_cpu()
        self.grad_hook.register_grad_hook()
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
        MemInfo.non_model_data_list.append(cuda_volume - last_model_data)
        self.grad_hook.remove_grad_hook()
        self._restore_param_data()

    def _clear_mem_info(self):
        MemInfo.model_data_list.clear()
        MemInfo.non_model_data_list.clear()
        MemInfo.unreleased_grad_flag.clear()
        MemInfo.unreleased_grad_volume = 0


    def _cast_buffers_to_cuda_dtype(self):
        for buffer in self.module.buffers():
            buffer.data = buffer.cuda()
            if torch.is_floating_point(buffer):
                buffer.data = buffer.data.to(self.dtype)

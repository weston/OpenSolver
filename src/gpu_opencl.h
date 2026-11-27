// The GPU kernels (postflop.cl, flop_table.cl) are written once, in OpenCL C 1.2 with these macros, and
// compiled for OpenCL with this header or for CUDA (NVRTC) with gpu_cuda.h: G and L are the global
// and work-group (shared) address spaces of pointers, SHARED declares a work-group array, FN marks a
// device function, KERNEL an entry point. SCRATCH_PARAM / SCRATCH_DECL give the kernel its dynamic
// work-group memory as `scratch` (an argument in OpenCL, the launch's shared memory in CUDA).
#pragma OPENCL FP_CONTRACT OFF
#define WSOLVER_GPU_OPTIONS 1
#define KERNEL __kernel
#define FN
#define G __global
#define L __local
#define SHARED __local
#define BARRIER() barrier(CLK_LOCAL_MEM_FENCE)
#define GROUP_X ((uint)get_group_id(0))
#define GROUP_Y ((uint)get_group_id(1))
#define TID ((uint)get_local_id(0))
#define WIDTH ((uint)get_local_size(0))
#define GLOBAL_X ((uint)get_global_id(0))
#define GLOBAL_Y ((uint)get_global_id(1))
#define SCRATCH_PARAM , __local float* scratch
#define SCRATCH_DECL
#define F2(a, b) ((float2)((a), (b)))
#define F3(a, b, c) ((float3)((a), (b), (c)))
#define F4(a, b, c, d) ((float4)((a), (b), (c), (d)))
#define U4(a, b, c, d) ((uint4)((a), (b), (c), (d)))
#define F2S(a) ((float2)(a))
#define F3S(a) ((float3)(a))
#define F4S(a) ((float4)(a))
#define AS_FLOAT(x) as_float(x)
#define AS_UINT(x) as_uint(x)
#define RINT(x) rint(x)
#define CLZ(x) clz(x)
#define POPCOUNT(x) popcount(x)
#define vload1(i, p) ((p)[i])
#define vstore1(v, i, p) ((p)[i] = (v))

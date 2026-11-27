// The GPU kernels compiled as CUDA C++ by NVRTC (src/cuda.cpp): see gpu_opencl.h for the dialect.
// This maps it onto CUDA, and gives CUDA's vector structs the OpenCL operators the kernels use.
#define WSOLVER_GPU_OPTIONS 1
typedef unsigned int uint;
typedef unsigned short ushort;
typedef unsigned char uchar;
#define KERNEL extern "C" __global__
#define FN __device__ inline
#define G
#define L
#define SHARED __shared__
#define BARRIER() __syncthreads()
#define GROUP_X ((uint)blockIdx.x)
#define GROUP_Y ((uint)blockIdx.y)
#define TID ((uint)threadIdx.x)
#define WIDTH ((uint)blockDim.x)
#define GLOBAL_X ((uint)(blockIdx.x * blockDim.x + threadIdx.x))
#define GLOBAL_Y ((uint)(blockIdx.y * blockDim.y + threadIdx.y))
#define SCRATCH_PARAM
#define SCRATCH_DECL extern __shared__ float scratch[];
#define F2(a, b) make_float2((a), (b))
#define F3(a, b, c) make_float3((a), (b), (c))
#define F4(a, b, c, d) make_float4((a), (b), (c), (d))
#define U4(a, b, c, d) make_uint4((a), (b), (c), (d))
#define F2S(a) make_float2((a), (a))
#define F3S(a) make_float3((a), (a), (a))
#define F4S(a) make_float4((a), (a), (a), (a))
#define AS_FLOAT(x) __uint_as_float(x)
#define AS_UINT(x) __float_as_uint(x)
#define RINT(x) rintf(x)
#define CLZ(x) ((uint)__clz(x))
#define POPCOUNT(x) ((uint)__popc(x))
#define vload1(i, p) ((p)[i])
#define vstore1(v, i, p) ((p)[i] = (v))

__device__ inline float2 vload2(uint i, const float* p) { return make_float2(p[2 * i], p[2 * i + 1]); }
__device__ inline float3 vload3(uint i, const float* p) { return make_float3(p[3 * i], p[3 * i + 1], p[3 * i + 2]); }
__device__ inline float4 vload4(uint i, const float* p) {
  return make_float4(p[4 * i], p[4 * i + 1], p[4 * i + 2], p[4 * i + 3]);
}
__device__ inline void vstore2(float2 v, uint i, float* p) { p[2 * i] = v.x; p[2 * i + 1] = v.y; }
__device__ inline void vstore3(float3 v, uint i, float* p) { p[3 * i] = v.x; p[3 * i + 1] = v.y; p[3 * i + 2] = v.z; }
__device__ inline void vstore4(float4 v, uint i, float* p) {
  p[4 * i] = v.x; p[4 * i + 1] = v.y; p[4 * i + 2] = v.z; p[4 * i + 3] = v.w;
}

// Component-wise operators, as OpenCL C has them built in (no contraction: nvrtc runs with --fmad=false).
#define WSOLVER_VEC_OPS(T, MAKE, ...)                                                                      \
  __device__ inline T operator+(T a, T b) { return MAKE(__VA_ARGS__, +); }                                 \
  __device__ inline T operator-(T a, T b) { return MAKE(__VA_ARGS__, -); }                                 \
  __device__ inline T operator*(T a, T b) { return MAKE(__VA_ARGS__, *); }                                 \
  __device__ inline T operator/(T a, T b) { return MAKE(__VA_ARGS__, /); }
#define WSOLVER_MAKE2(x, y, op) make_float2(a.x op b.x, a.y op b.y)
#define WSOLVER_MAKE3(x, y, z, op) make_float3(a.x op b.x, a.y op b.y, a.z op b.z)
#define WSOLVER_MAKE4(x, y, z, w, op) make_float4(a.x op b.x, a.y op b.y, a.z op b.z, a.w op b.w)
WSOLVER_VEC_OPS(float2, WSOLVER_MAKE2, x, y)
WSOLVER_VEC_OPS(float3, WSOLVER_MAKE3, x, y, z)
WSOLVER_VEC_OPS(float4, WSOLVER_MAKE4, x, y, z, w)
__device__ inline float2 operator*(float s, float2 v) { return make_float2(s * v.x, s * v.y); }
__device__ inline float3 operator*(float s, float3 v) { return make_float3(s * v.x, s * v.y, s * v.z); }
__device__ inline float4 operator*(float s, float4 v) { return make_float4(s * v.x, s * v.y, s * v.z, s * v.w); }
__device__ inline float2 operator*(float2 v, float s) { return make_float2(v.x * s, v.y * s); }
__device__ inline float3 operator*(float3 v, float s) { return make_float3(v.x * s, v.y * s, v.z * s); }
__device__ inline float4 operator*(float4 v, float s) { return make_float4(v.x * s, v.y * s, v.z * s, v.w * s); }
__device__ inline float2 operator/(float2 v, float s) { return make_float2(v.x / s, v.y / s); }
__device__ inline float3 operator/(float3 v, float s) { return make_float3(v.x / s, v.y / s, v.z / s); }
__device__ inline float4 operator/(float4 v, float s) { return make_float4(v.x / s, v.y / s, v.z / s, v.w / s); }
__device__ inline void operator+=(float2& a, float2 b) { a = a + b; }
__device__ inline void operator+=(float3& a, float3 b) { a = a + b; }
__device__ inline void operator+=(float4& a, float4 b) { a = a + b; }
__device__ inline void operator-=(float2& a, float2 b) { a = a - b; }
__device__ inline void operator-=(float3& a, float3 b) { a = a - b; }
__device__ inline void operator-=(float4& a, float4 b) { a = a - b; }
__device__ inline float2 fma(float2 a, float2 b, float2 c) { return make_float2(fmaf(a.x, b.x, c.x), fmaf(a.y, b.y, c.y)); }
__device__ inline float3 fma(float3 a, float3 b, float3 c) {
  return make_float3(fmaf(a.x, b.x, c.x), fmaf(a.y, b.y, c.y), fmaf(a.z, b.z, c.z));
}
__device__ inline float4 fma(float4 a, float4 b, float4 c) {
  return make_float4(fmaf(a.x, b.x, c.x), fmaf(a.y, b.y, c.y), fmaf(a.z, b.z, c.z), fmaf(a.w, b.w, c.w));
}

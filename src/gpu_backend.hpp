#pragma once
// What the discrete-GPU solve (gpu_solve.cpp) needs from a compute API: OpenCL (opencl.cpp) or CUDA
// (cuda.cpp). Each loads its driver at run time, so the solver builds without an SDK and starts on a
// machine without the driver; and both compile the same kernel source (postflop.cl, flop_table.cl).
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace pio {
namespace gpu {

// Kernel builds: plain float32 state, bfloat16 regrets, bfloat16 regrets with fp16 strategy sums.
enum Variant { kFloatState, kBfloatRegrets, kBfloatRegretsHalfSums, kVariants };

using Buf = uint64_t;  // a device allocation: a cl_mem or a CUdeviceptr; 0 is none

struct Arg {
  bool buffer;
  Buf buf;
  uint8_t bytes[64];
  size_t size;
};
inline Arg arg(Buf b) {
  Arg a{};
  a.buffer = true;
  a.buf = b;
  a.size = sizeof(Buf);
  return a;
}
template <class T>
Arg value(const T& v) {
  static_assert(sizeof(T) <= sizeof(Arg::bytes), "kernel argument too large");
  Arg a{};
  a.buffer = false;
  __builtin_memcpy(a.bytes, &v, sizeof(T));
  a.size = sizeof(T);
  return a;
}

class Backend {
 public:
  virtual ~Backend() = default;
  virtual const char* api() const = 0;  // "OpenCL", "CUDA"
  std::string device_name;
  uint64_t memory = 0, max_alloc = 0;
  std::mutex mu;  // one user of the device at a time (flop tables are built from pool threads)

  // A kernel of a build, compiled on first use; its widest work-group and the dynamic work-group
  // memory it can take.
  virtual void* kernel(Variant variant, const char* name) = 0;
  virtual size_t max_width(void* kernel) = 0;
  virtual size_t local_limit(void* kernel) = 0;

  virtual Buf alloc(size_t bytes) = 0;  // throws std::bad_alloc when the device is out of memory
  virtual void release(Buf b) = 0;
  virtual void write(Buf b, size_t offset, const void* data, size_t bytes, bool blocking) = 0;
  virtual void read(Buf b, size_t offset, void* data, size_t bytes, bool blocking) = 0;
  virtual void zero(Buf b, size_t bytes) = 0;
  // groups_x x groups_y work-groups of `width` work-items, `scratch` bytes of dynamic work-group memory.
  virtual void launch(void* kernel, const char* name, size_t groups_x, size_t groups_y, size_t width, size_t scratch,
                      const std::vector<Arg>& args) = 0;
  virtual void flush() = 0;
  virtual void finish() = 0;
};

// The selected device (CUDA first, then OpenCL; WSOLVER_GPU_BACKEND=opencl or cuda picks one), or
// null with the reason in `error`.
Backend* backend(std::string* error = nullptr);
std::unique_ptr<Backend> make_opencl_backend(std::string& error);
std::unique_ptr<Backend> make_cuda_backend(std::string& error);
// The kernel source of postflop.cl and flop_table.cl, with gpu_opencl.h or gpu_cuda.h in front.
extern const char opencl_kernel_source[];
extern const char cuda_kernel_source[];

// WSOLVER_GPU_PROFILE=1 (a developer switch, AGENTS.md): each kernel's GPU time, the wall time of each
// setup step and state copy, and the plan's shape, on stderr at exit. It waits for each command, so
// only the shares mean anything, not the total.
struct Profile {
  bool on = std::getenv("WSOLVER_GPU_PROFILE") != nullptr;
  std::map<std::string, std::pair<double, long>> spent;  // seconds, calls
  void add(const std::string& what, double seconds) {
    auto& entry = spent[what];
    entry.first += seconds;
    ++entry.second;
  }
  ~Profile() {
    for (const auto& [what, sn] : spent)
      std::fprintf(stderr, "GPU profile: %-30s %9.1f ms %6ld calls\n", what.c_str(), sn.first * 1e3, sn.second);
  }
};
inline Profile& profile() {
  static Profile p;
  return p;
}
struct ProfileScope {
  Backend& b;
  const char* what;
  std::chrono::steady_clock::time_point t0;
  ProfileScope(Backend& backend, const char* name) : b(backend), what(name) {
    if (!profile().on) return;
    b.finish();
    t0 = std::chrono::steady_clock::now();
  }
  ~ProfileScope() {
    if (!profile().on) return;
    b.finish();
    profile().add(what, std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count());
  }
};

}  // namespace gpu
}  // namespace pio

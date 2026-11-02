// Thread pool with nested parallel_for (callers help run their own job; idle workers take the
// most recently pushed job first, which keeps execution depth-first) and a per-thread bump arena.
#pragma once
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace pio {

class ThreadPool {
 public:
  explicit ThreadPool(int threads = 0);
  ~ThreadPool();
  void set_threads(int threads);  // total threads including the caller; <=0 -> hardware
  int threads() const { return nthreads_; }

  // Runs f(i) for i in [0, n). Safe to call recursively from inside f.
  void parallel_for(int n, const std::function<void(int)>& f);

 private:
  struct Job {
    const std::function<void(int)>* fn;
    int n;
    int width;  // parallel work already exposed by this job and its ancestors
    std::atomic<int> next{0};
    std::atomic<int> done{0};
    std::atomic<int> active{0};
  };
  void start(int workers);
  void stop_all();
  void worker_loop();
  bool run_some(Job* j);
  Job* pick_job(size_t min_index);

  std::vector<std::thread> workers_;
  std::mutex m_;
  std::condition_variable cv_;
  std::vector<Job*> jobs_;
  bool quit_ = false;
  int nthreads_ = 1;
};

ThreadPool& pool();

// ---- Arena -------------------------------------------------------------------------------
class Arena {
 public:
  struct Mark {
    size_t block, off;
  };
  float* alloc(size_t n);  // 64-byte aligned, uninitialized
  float* alloc_zero(size_t n);
  Mark mark() const { return {cur_, off_}; }
  void reset(Mark m) {
    cur_ = m.block;
    off_ = m.off;
  }

 private:
  static constexpr size_t kBlockFloats = size_t(1) << 22;  // 16 MB blocks
  struct Block {
    std::unique_ptr<float[]> mem;
    size_t size;
  };
  std::vector<Block> blocks_;
  size_t cur_ = 0, off_ = 0;
};

Arena& arena();  // thread-local

struct ArenaScope {
  Arena& a;
  Arena::Mark m;
  ArenaScope() : a(arena()), m(a.mark()) {}
  ~ArenaScope() { a.reset(m); }
};

}  // namespace pio

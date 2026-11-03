"""有界跟单执行器；只承担网络任务调度，不读写业务状态。"""

import threading
from concurrent.futures import ThreadPoolExecutor


class BoundedDispatcher:
    """同账号最多一个在途任务，已完成任务由主循环回收后释放名额。

    ``on_done`` 是无参数的轻量唤醒回调。关闭后禁止继续提交，但已在运行
    的网络请求不会被强制中断；调用方仍可回收并保存其最终结果。
    """

    def __init__(self, max_workers=2, on_done=None):
        self.max_workers = max(1, int(max_workers))
        self._on_done = on_done
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="ps38-follow-attempt"
        )
        self._active = {}
        self._keys = set()
        self._accounts = set()
        self._closed = False

    def submit(self, key, account_id, fn, *args, **kwargs):
        """有可用名额才提交，否则返回 None；内部等待队列不会无限增长。"""
        key = str(key or "").strip()
        account_id = str(account_id or "").strip()
        if not key or not account_id:
            return None
        with self._lock:
            if (
                self._closed
                or key in self._keys
                or account_id in self._accounts
                or len(self._active) >= self.max_workers
            ):
                return None
            self._keys.add(key)
            self._accounts.add(account_id)
            try:
                future = self._executor.submit(fn, *args, **kwargs)
            except BaseException:
                self._keys.discard(key)
                self._accounts.discard(account_id)
                raise
            self._active[future] = (key, account_id)
        # 已完成 Future 会同步执行回调；放在锁外以允许唤醒调用方安全重入。
        future.add_done_callback(self._notify_done)
        return future

    def _notify_done(self, future):
        if self._on_done is not None:
            try:
                self._on_done()
            except Exception:
                # 唤醒失败不能改变执行结果；主循环仍可定时 poll_completed。
                pass

    def poll_completed(self):
        """统一回收成功、异常及取消的 Future，并释放对应账号与任务键。"""
        completed = []
        with self._lock:
            for future, (key, account_id) in list(self._active.items()):
                if not future.done():
                    continue
                del self._active[future]
                self._keys.discard(key)
                self._accounts.discard(account_id)
                completed.append((key, account_id, future))
        return completed

    def is_key_active(self, key):
        with self._lock:
            return str(key or "").strip() in self._keys

    def active_count(self):
        with self._lock:
            return len(self._active)

    def active_accounts(self):
        with self._lock:
            return set(self._accounts)

    def available_slots(self):
        with self._lock:
            return max(0, self.max_workers - len(self._active))

    def shutdown(self, wait=False):
        """停止接收任务并取消未开始的请求；运行中请求由业务停止信号退出。"""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=bool(wait), cancel_futures=True)

"""持久累计准入预算；恢复不会重置额度，缓存复用释放冷调用预留。"""
import time


class Budget:
    def __init__(self, state, *, wall_seconds, case_calls=48, full_calls=6, ai_calls=10, profile_calls=2):
        limits = {'wall_seconds': wall_seconds, 'case': case_calls, 'full': full_calls, 'ai': ai_calls, 'profile':profile_calls}
        if any(type(v) is not int or v <= 0 for v in limits.values()):
            raise ValueError('预算必须为正整数')
        if 'budget' not in state:
            state['budget'] = {'limits': limits, 'deadline': time.time() + wall_seconds, 'reservations': {}}
        self.state = state['budget']
        if self.state['limits'] != limits:
            raise ValueError('恢复时不能隐式改变累计预算；新预算需要新 campaign')

    def expired(self):
        return time.time() >= self.state['deadline']

    def used(self, kind):
        return sum(r['kind'] == kind and not r.get('reused', False) for r in self.state['reservations'].values())

    def reserve(self, identity, kind):
        if identity in self.state['reservations']:
            return True
        if self.expired() or (kind in self.state['limits'] and self.used(kind) >= self.state['limits'][kind]):
            return False
        if kind in {'case', 'full', 'ai', 'profile'}:
            self.state['reservations'][identity] = {'kind': kind, 'reserved_wall': time.time()}
        return True

    def reused(self, identity):
        if identity in self.state['reservations']:
            self.state['reservations'][identity]['reused'] = True

    def snapshot(self):
        return {'remaining_wall_seconds': max(0, self.state['deadline'] - time.time()),
            'used': {kind: self.used(kind) for kind in ['case', 'full', 'ai', 'profile']},
            'limits': self.state['limits']}

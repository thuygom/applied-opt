import dataclasses
import unittest

from applied_opt.cache import CacheKeyError, CachePolicy, CacheScope, MemoryCache, exact_key


def scope(**changes):
    default = CacheScope("tenant-a", "subject-a", "auth-v1", "data-v1", "model-v1", "prompt-v1", "2026-01-01")
    return dataclasses.replace(default, **changes)


class CacheTests(unittest.TestCase):
    def test_every_scope_dimension_changes_key(self):
        baseline = exact_key("lookup", "1", scope(), {"query": "x"}, {})
        for field in dataclasses.fields(CacheScope):
            changed = scope(**{field.name: "different"})
            self.assertNotEqual(baseline, exact_key("lookup", "1", changed, {"query": "x"}, {}))
        self.assertNotEqual(baseline, exact_key("lookup", "2", scope(), {"query": "x"}, {}))
        self.assertNotEqual(baseline, exact_key("lookup", "1", scope(), {"query": "y"}, {}))
        self.assertNotEqual(baseline, exact_key("lookup", "1", scope(), {"query": "x"}, {"dep": 1}))
        self.assertNotEqual(baseline, exact_key("lookup", "1", scope(), {"query": "x"}, {}, route_revision="different"))

    def test_key_canonical_order_and_type_safety(self):
        self.assertEqual(exact_key("s", "1", scope(), {"a": 1, "b": 2}, {}),
                         exact_key("s", "1", scope(), {"b": 2, "a": 1}, {}))
        for unsafe in ({1: "x"}, {"n": float("nan")}, {"x": object()}, {"x": (1, 2)}):
            with self.assertRaises(CacheKeyError):
                exact_key("s", "1", scope(), unsafe, {})
        with self.assertRaises(CacheKeyError):
            exact_key("s", "1", scope(subject_id=""), {}, {})
        with self.assertRaises(ValueError):
            CachePolicy()
        with self.assertRaises(ValueError):
            CachePolicy(deterministic=True, read_only=False)

    def test_ttl_bounded_lru_and_deep_copy(self):
        now = [0.0]
        cache = MemoryCache(2, clock=lambda: now[0])
        original = {"items": [1]}
        cache.put("a", original, ttl_s=10, scope=scope())
        original["items"].append(2)
        _, first = cache.get("a")
        first["items"].append(3)
        self.assertEqual(cache.get("a"), (True, {"items": [1]}))
        cache.put("b", None, ttl_s=10, scope=scope())
        cache.get("a")
        cache.put("c", 3, ttl_s=10, scope=scope())
        self.assertFalse(cache.get("b")[0])
        self.assertEqual(len(cache), 2)
        now[0] = 10.0
        self.assertEqual(cache.get("a"), (False, None))
        self.assertEqual(len(cache), 0)

    def test_scoped_invalidation(self):
        cache = MemoryCache()
        cache.put("a", 1, ttl_s=10, scope=scope())
        cache.put("b", 2, ttl_s=10, scope=scope(subject_id="other"))
        cache.put("c", 3, ttl_s=10, scope=scope(tenant_id="other"))
        self.assertEqual(cache.invalidate(tenant_id="tenant-a", subject_id="subject-a"), 1)
        self.assertTrue(cache.get("b")[0])
        self.assertEqual(cache.invalidate(tenant_id="tenant-a"), 1)
        self.assertTrue(cache.get("c")[0])


if __name__ == "__main__":
    unittest.main()

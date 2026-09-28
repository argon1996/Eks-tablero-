import json
import unittest
from pathlib import Path
from unittest.mock import patch

from eks_console import backend


ROOT = Path(__file__).resolve().parents[1]


class RestrictedQueryTests(unittest.TestCase):
    def setUp(self):
        self.demo = backend.DEMO
        backend.DEMO = False
        backend.KUBE_CONFIG_CACHE.update(data=None, expires=0.0)

    def tearDown(self):
        backend.DEMO = self.demo
        backend.KUBE_CONFIG_CACHE.update(data=None, expires=0.0)

    def test_global_namespace_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "namespace exacto"):
            backend.scope("*")

    @patch.object(backend, "kubectl")
    def test_kubeconfig_is_reused_for_immediate_queries(self, kubectl):
        kubectl.return_value = json.dumps({
            "current-context": "qa",
            "contexts": [{"name": "qa", "context": {"cluster": "eks-qa"}}],
        })
        self.assertEqual(backend.resolve_context("qa"), "qa")
        self.assertEqual(backend.resolve_context("qa"), "qa")
        kubectl.assert_called_once()

    @patch.object(backend, "resolve_context", return_value="qa")
    @patch.object(backend, "kubectl")
    def test_fast_pod_read_does_not_wait_for_metrics(self, kubectl, _resolve):
        kubectl.return_value = json.dumps({"items": [{
            "metadata": {"name": "pod-a", "namespace": "team-a", "uid": "u1", "labels": {}},
            "spec": {"containers": [{"name": "app", "image": "local/app:1"}]},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": "app", "ready": True, "restartCount": 0}]},
        }]})
        result = backend.list_pods("team-a", "qa", include_metrics=False)
        self.assertEqual(len(result["pods"]), 1)
        command = kubectl.call_args.args
        self.assertIn("get", command)
        self.assertNotIn("top", command)
        self.assertIn("-n", command)
        self.assertIn("team-a", command)

    def test_source_has_no_cluster_wide_discovery(self):
        source = (ROOT / "eks_console" / "backend.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("['-a']", source)
        self.assertNotIn("list-clusters", source)
        self.assertNotRegex(source, r"kubectl\([^\n]*['\"]namespaces['\"]")


if __name__ == "__main__":
    unittest.main()

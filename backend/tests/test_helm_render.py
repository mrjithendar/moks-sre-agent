"""What each component actually receives once credentials are split."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy/helm/moks"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm is not installed")


def render(values_file: str, *sets: str) -> list[dict]:
    args = ["helm", "template", "moks", str(CHART), "-n", "moks-sre", "-f", str(CHART / values_file)] + ([] if values_file == "values-hetzner.yaml" else ["--set", "image.tag=test"])
    for s in sets:
        args += ["--set", s]
    return [d for d in yaml.safe_load_all(subprocess.run(args, check=True, capture_output=True, text=True).stdout) if d]


def pods(docs):
    for d in docs:
        spec = (d.get("spec") or {})
        tpl = (spec.get("jobTemplate", {}).get("spec", spec)).get("template")
        if tpl:
            yield d["metadata"]["name"], tpl["spec"]


def secrets(pod) -> set[str]:
    found = set()
    for c in pod.get("containers", []) + pod.get("initContainers", []):
        found |= {e["secretRef"]["name"] for e in c.get("envFrom", []) if "secretRef" in e}
        found |= {e["valueFrom"]["secretKeyRef"]["name"] for e in c.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})}
    return found


SPLIT = ("secrets.split=true", "backup.enabled=true", "backup.bucket=b", "velero.enabled=true")
# mk4 uses the shared CloudNativePG database and has no Velero yet (values-hetzner.yaml), so the checks of the bundled
# database, its backup CronJob and the Velero roles turn those on.
BUNDLED = "postgres.bundled=true"
VELERO = "velero.enabled=true"
# mk4 turns secret sync on; the checks of the posture without it switch it off again.
SYNC_OFF = "secretsSync.enabled=false"
TERMINAL = (
    "terminal.enabled=true",
    "terminal.image.digest=sha256:" + "0" * 64,
    "terminal.profiles[0].name=eks,terminal.profiles[0].label=eks,terminal.profiles[0].environment=roboshop-demo",
    "terminal.profiles[0].account=1,terminal.profiles[0].roleArn=arn,terminal.profiles[0].regions[0]=us-east-1",
)


@pytest.mark.parametrize("values", ["values-hetzner.yaml", "values-eks.yaml"])
def test_default_render_changes_nothing_for_existing_installs(values):
    docs = render(values)
    by = dict(pods(docs))
    shared = {"moks-rag-client"} if values == "values-hetzner.yaml" else set()  # mk4 turns sharedKnowledge on (optional keys)
    assert secrets(by["moks-api"]) == {"moks-secrets"} | shared and by["moks-api"]["serviceAccountName"] == "moks"
    assert by["moks-api"]["initContainers"][0]["name"] == "migrate"
    assert not any(d["kind"] == "Job" for d in docs)


def test_the_default_group_is_the_whole_chart():
    assert {(d["kind"], d["metadata"]["name"]) for d in render("values-hetzner.yaml")} == {
        (d["kind"], d["metadata"]["name"]) for d in render("values-hetzner.yaml", "resourceGroup=all")}


def test_a_mistyped_group_is_refused_rather_than_rendering_nothing():
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render("values-hetzner.yaml", "resourceGroup=frontend")
    assert "is not a resource group" in refused.value.stderr


def test_config_changes_still_roll_consumers_though_only_foundation_renders_the_configmaps():
    assert [d["metadata"]["name"] for d in render("values-hetzner.yaml", "resourceGroup=api") if d["kind"] == "ConfigMap"] == []
    assert {"moks-config", "moks-seed"} <= {d["metadata"]["name"] for d in render("values-hetzner.yaml", "resourceGroup=foundation") if d["kind"] == "ConfigMap"}

    def checksum(*sets):
        return of(render("values-hetzner.yaml", "resourceGroup=api", *sets), "Deployment", "moks-api")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    whole = of(render("values-hetzner.yaml"), "Deployment", "moks-api")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert checksum() == whole and checksum("config.logLevel=DEBUG") != whole


def test_each_component_gets_only_its_secret():
    by = dict(pods(render("values-hetzner.yaml", *SPLIT, SYNC_OFF, BUNDLED)))
    assert secrets(by["moks-api"]) == {"moks-api-secrets", "moks-rag-client"}  # moks-rag's URL and token (sharedKnowledge), nothing else
    assert secrets(by["moks-worker"]) == {"moks-worker-secrets", "moks-rag-client"}
    assert secrets(by["moks-executor"]) == {"moks-executor-secrets"}
    assert secrets(by["moks-backup"]) == {"moks-backup"}
    assert not any("moks-migrate-secrets" in secrets(p) for n, p in by.items() if not n.startswith("moks-migrate"))
    assert not any("moks-backup" in secrets(p) for n, p in by.items() if n != "moks-backup")
    assert not any("moks-executor-secrets" in secrets(p) for n, p in by.items() if n != "moks-executor")


def test_only_the_job_holds_the_schema_owner():
    by = dict(pods(render("values-hetzner.yaml", *SPLIT)))
    for name in ("moks-api", "moks-worker", "moks-executor"):
        assert [c["name"] for c in by[name]["initContainers"]] == ["wait-schema"]
    job = next(p for n, p in by.items() if n.startswith("moks-migrate"))
    assert job["automountServiceAccountToken"] is False and "db-grants" in job["containers"][0]["command"][2]


def test_identities_are_separate_and_nobody_reads_secrets():
    docs = render("values-hetzner.yaml", *SPLIT, SYNC_OFF)
    by = dict(pods(docs))
    assert len({by[n]["serviceAccountName"] for n in ("moks-api", "moks-worker", "moks-executor")}) == 3
    for d in docs:
        if d["kind"] in ("Role", "ClusterRole") and d["metadata"]["name"].startswith("moks"):
            for rule in d["rules"]:
                if "secrets" in rule.get("resources", []):
                    assert not {"get", "list", "watch"} & set(rule["verbs"]), d["metadata"]["name"]


def test_chart_renders_for_eks_split():
    assert any(d["kind"] == "Job" for d in render("values-eks.yaml", "secrets.split=true"))


def test_worker_pools_scale_each_class_separately():
    pools = [f"worker.pools[{i}].name={n},worker.pools[{i}].classes={n},worker.pools[{i}].replicas=1" for i, n in enumerate(["detection", "knowledge"])]
    names = {n for n, _ in pods(render("values-eks.yaml", *pools))}
    assert {"moks-worker-detection", "moks-worker-knowledge"} <= names and "moks-worker" not in names


def test_eks_routes_through_istio_without_traefik():
    docs = render("values-eks.yaml")
    route = next(d for d in docs if d["kind"] == "VirtualService")
    assert route["spec"]["gateways"] == ["istio-ingress/public"] and route["spec"]["hosts"] == ["eks.jithendar.com"]
    # The terminal is off on EKS (values-eks.yaml); the next test turns it on and checks its route comes first.
    assert [r["name"] for r in route["spec"]["http"]] == ["portal"]
    assert not any(d["kind"] in ("Ingress", "Middleware") for d in docs)


def test_istio_sends_terminal_traffic_to_the_gateway_first():
    route = next(d for d in render("values-eks.yaml", *TERMINAL) if d["kind"] == "VirtualService")
    first, second = route["spec"]["http"]
    assert first["match"] == [{"uri": {"prefix": "/moks/api/terminal"}}] and second["match"] == [{"uri": {"prefix": "/moks"}}]
    assert first["route"][0]["destination"]["host"] == "moks-terminal.moks-sre.svc.cluster.local"


def test_minikube_is_strict_and_routed_by_delegates_without_gateway_policies():
    docs = render("values-hetzner.yaml")
    # Only the namespace's allow-known-callers (mesh.authorization), none of istio.mtls's gateway policies.
    assert [d["metadata"]["name"] for d in docs if d["kind"] == "AuthorizationPolicy"] == ["allow-known-callers"]
    assert not any(d["kind"] == "Ingress" or d["apiVersion"].startswith("traefik.io/") for d in docs)
    routes = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "VirtualService"}
    assert set(routes) == {"moks"}
    assert not any("hosts" in r or "gateways" in r for r in routes.values())   # delegates of miniKube's routing table
    peer = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "PeerAuthentication"}
    assert peer["default"] == {"mtls": {"mode": "STRICT"}}
    # mk4: the Istio gateway is the only edge and Prometheus scrapes through its sidecar, so no port takes plain text.
    # Every name still renders (Argo CD never prunes here, so a dropped policy would keep its old exceptions live).
    assert set(peer) == {"default", "moks-metrics", "moks-reranker-metrics", "moks-embedder-metrics"}
    assert all("portLevelMtls" not in spec and spec["mtls"] == {"mode": "STRICT"} for spec in peer.values())
    assert peer["moks-metrics"]["selector"] == {"matchLabels": {"app": "moks"}}
    # An edge or a scraper outside the mesh gets exactly its ports back.
    legacy = {d["metadata"]["name"]: d["spec"] for d in render("values-hetzner.yaml", "mesh.edgeMtls=false", "mesh.metricsMtls=false")
              if d["kind"] == "PeerAuthentication"}
    assert legacy["moks-metrics"]["portLevelMtls"] == {9464: {"mode": "PERMISSIVE"}, 8080: {"mode": "PERMISSIVE"}}
    assert legacy["moks-reranker-metrics"]["portLevelMtls"] == {9464: {"mode": "PERMISSIVE"}}
    assert legacy["moks-embedder-metrics"]["portLevelMtls"] == {9464: {"mode": "PERMISSIVE"}}
    policy = next(d for d in docs if d["kind"] == "AuthorizationPolicy")
    assert policy["metadata"]["annotations"] == {"istio.io/dry-run": "true"}
    assert policy["spec"] == {"action": "ALLOW", "rules": [{"from": [{"source": {"namespaces": ["moks-sre", "istio-ingress", "monitoring"]}}]}]}
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}
    assert "traefik" not in str(policies)

def test_prometheus_can_scrape_every_moks_pod_through_the_mesh():
    """With mesh.metricsMtls Prometheus's sidecar sends mTLS only to pod addresses a Service names; a pod with no Service on
    :9464 is scraped in plain text and refused (the reranker on mk4 until 2026-11-20)."""
    docs = render("values-hetzner.yaml", "embedder.enabled=true", "embedder.image.tag=test")
    headless = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "Service" and d["spec"].get("clusterIP") == "None"}
    scraped = {d["spec"]["template"]["metadata"]["labels"]["app"] for d in docs if d["kind"] == "Deployment"
               and (d["spec"]["template"]["metadata"].get("annotations") or {}).get("prometheus.io/port") == "9464"
               or d["kind"] == "Deployment" and any(p.get("containerPort") == 9464 for c in d["spec"]["template"]["spec"]["containers"] for p in c.get("ports", []))}
    assert {"moks", "moks-reranker", "moks-embedder"} <= scraped
    for app in scraped:
        assert any(s["selector"] == {"app": app} and [p["port"] for p in s["ports"]] == [9464] for s in headless.values()), app


def test_minikube_sidecars_reach_istiod_only_from_already_isolated_pods():
    docs = render("values-hetzner.yaml")
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}
    mesh = policies.pop("istio-sidecars")
    selected = set(mesh["podSelector"]["matchExpressions"][0]["values"])
    isolated = {p["podSelector"]["matchLabels"]["app"] for p in policies.values()
                if set(p.get("policyTypes", [])) == {"Ingress", "Egress"} and "app" in p["podSelector"].get("matchLabels", {})}
    # Session pods share moks-sre but opt out of injection (terminal/pods.py, diagnostics/service.py): no sidecar, no istiod.
    sessions = {"moks-diagnostic", "moks-terminal"}
    assert sessions <= isolated and not sessions & selected
    isolated -= sessions
    # moks-postgres is always selected; mk4 does not run it, so that matches nothing.
    assert isolated <= selected and selected - isolated <= {"moks-postgres"}
    # DNS too: the proxy resolves istiod by name, and postgres and the reranker have no DNS rule of their own.
    assert mesh["egress"] == [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                       "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                               "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]},
                              {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "istio-system"}}}],
                               "ports": [{"port": 15012, "protocol": "TCP"}]}]
    assert {"port": 15090, "protocol": "TCP"} in mesh["ingress"][0]["ports"]


def test_mesh_and_gateway_mtls_are_exclusive():
    with pytest.raises(subprocess.CalledProcessError):
        render("values-eks.yaml", "mesh.enabled=true")


def test_eks_is_strict_and_denies_by_default():
    docs = render("values-eks.yaml")
    assert next(d for d in docs if d["kind"] == "PeerAuthentication")["spec"]["mtls"]["mode"] == "STRICT"
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "AuthorizationPolicy"}
    assert policies["allow-nothing"] == {}
    api, db = policies["moks"]["rules"][0], policies["moks-postgres"]["rules"][0]
    assert api["from"][0]["source"]["principals"] == ["cluster.local/ns/istio-ingress/sa/istio-ingressgateway"]
    assert api["to"][0]["operation"]["ports"] == ["8080"]
    assert db["from"][0]["source"]["namespaces"] == ["moks-sre"] and db["to"][0]["operation"]["ports"] == ["5432"]


def test_meshed_postgres_can_reach_istiod_when_policy_is_enforced():
    docs = render("values-eks.yaml")
    egress = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "moks-postgres")["spec"]["egress"]
    assert {"port": 15012, "protocol": "TCP"} in egress[1]["ports"]
    assert next(d for d in render("values-hetzner.yaml", BUNDLED) if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "moks-postgres")["spec"]["egress"] == []


def test_mtls_needs_a_gateway_identity():
    with pytest.raises(subprocess.CalledProcessError):
        render("values-eks.yaml", "istio.gatewayPrincipal=")


def test_minikube_keeps_one_worker_with_reserved_capacity():
    by = dict(pods(render("values-hetzner.yaml")))
    env = {e["name"]: e.get("value") for e in by["moks-worker"]["containers"][0]["env"]}
    assert "detection=2" in env["MOKS_WORKER_CAPACITY"] and "moks-worker-detection" not in by


def test_mk4_always_pulls_latest():
    for name, pod in pods(render("values-hetzner.yaml")):
        for c in pod["containers"]:
            if c["image"].split("/")[-1].startswith("moks"):
                assert c["image"].endswith(("/moks:latest", "/moks-crewai:latest", "/moks-reranker:latest", "/moks-embedder:latest")) and c["imagePullPolicy"] == "Always", name


def test_the_chart_ships_the_consistent_dump():
    docs = render("values-hetzner.yaml", *SPLIT, BUNDLED)
    by = dict(pods(docs))
    dump = by["moks-backup"]["initContainers"][0]["command"][2]
    assert "consistent-dump.sql" in dump and "--manifest" in by["moks-backup"]["containers"][0]["args"]
    cm = next(d for d in docs if d["metadata"]["name"] == "moks-backup-scripts")
    assert "--snapshot" in cm["data"]["consistent-dump.sql"]


@pytest.mark.parametrize("digest", ["latest", "sha256:abc", "99f94e21"])
def test_a_malformed_digest_is_refused(digest):
    done = subprocess.run(["helm", "template", "moks", str(CHART), "--set", f"image.digest={digest}"], capture_output=True, text=True)
    assert done.returncode != 0 and "image.digest must be" in done.stderr


def test_mk4_has_velero_access_and_no_node_agent():
    docs = render("values-hetzner.yaml", VELERO)
    assert {"moks-velero-reader", "moks-executor-velero"} <= {d["metadata"]["name"] for d in docs if d["kind"] == "Role"}
    values = yaml.safe_load((CHART.parents[1] / "velero/values-r2-minikube.yaml").read_text())
    assert values["deployNodeAgent"] is False and values["configuration"]["defaultVolumesToFsBackup"] is False


def test_argo_can_deploy_velero_in_either_order():
    docs = render("values-hetzner.yaml", VELERO)
    ns = next(d for d in docs if d["kind"] == "Namespace" and d["metadata"]["name"] == "velero")
    assert ns["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"  # before the Roles inside it
    assert "Prune=false" in ns["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
    project, app = yaml.safe_load_all((CHART.parents[1] / "argocd/velero.yaml").read_text())
    assert app["spec"]["project"] == project["metadata"]["name"]
    # Automated sync is allowed, pruning never is: the namespace holds backups' Secrets and Velero's state.
    assert app["spec"]["syncPolicy"].get("automated", {}).get("prune", False) is False
    assert "Prune=false" in app["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
    values = app["spec"]["sources"][0]["helm"]["valueFiles"][0].replace("$values/", "")
    assert (CHART.parents[2] / values).exists()
    assert {s["repoURL"] for s in app["spec"]["sources"]} <= set(project["spec"]["sourceRepos"])
    assert yaml.safe_load((CHART.parents[2] / values).read_text())["upgradeCRDs"] is False


def test_velero_project_allows_everything_the_chart_renders():
    out = subprocess.run(["helm", "template", "velero", "velero", "--repo", "https://vmware-tanzu.github.io/helm-charts", "--version", "12.2.2", "-n", "velero",
                          "--include-crds", "-f", str(CHART.parents[1] / "velero/values-r2-minikube.yaml")], capture_output=True, text=True)
    if out.returncode:
        pytest.skip("the Velero chart repository is unreachable")
    project = next(yaml.safe_load_all((CHART.parents[1] / "argocd/velero.yaml").read_text()))
    allowed = {w["kind"] for w in project["spec"]["clusterResourceWhitelist"] + project["spec"]["namespaceResourceWhitelist"]}
    rendered = {d["kind"] for d in yaml.safe_load_all(out.stdout) if d}
    assert rendered <= allowed, rendered - allowed


TERMINAL = ("terminal.enabled=true", "terminal.image.digest=sha256:" + "a" * 64, "terminal.profiles[0].name=ro", "terminal.kubernetes.namespaces={moks-sre-lab}", "terminal.network.apiServer=true")


def of(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


# The chart's own defaults (values.yaml). values-hetzner.yaml is mk4, which turns the terminal on on purpose:
# see test_mk4_opts_into_the_terminal_with_https_only.
def test_terminals_are_off_by_default():
    assert not [d for d in render("values.yaml") if "terminal" in d["metadata"]["name"]]


def test_a_bad_digest_no_image_or_no_profile_is_refused():
    with pytest.raises(subprocess.CalledProcessError):
        render("values.yaml", "terminal.enabled=true", "terminal.image.digest=latest", "terminal.profiles[0].name=ro")
    with pytest.raises(subprocess.CalledProcessError):
        render("values.yaml", "terminal.enabled=true", "terminal.image.tag=", "terminal.profiles[0].name=ro")
    with pytest.raises(subprocess.CalledProcessError):
        render("values.yaml", "terminal.enabled=true", "terminal.image.digest=sha256:" + "a" * 64)


def test_mk4_pulls_every_moks_image_from_harbor_as_latest():
    docs = render("values-hetzner.yaml")
    images, secrets = set(), set()
    for doc in docs:
        spec = doc.get("spec", {})
        pod = spec.get("jobTemplate", {}).get("spec", spec).get("template", {}).get("spec")
        if not pod:
            continue
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            for env in container.get("env", []):
                if env["name"] == "MOKS_TERMINAL_IMAGE":
                    images.add(env["value"])
            if "/jithendar/moks" in container["image"]:
                images.add(container["image"])
                secrets.add(tuple(s["name"] for s in pod.get("imagePullSecrets", [])))
    assert images and all(i.startswith("registry.jithendar.com/jithendar/moks") and i.endswith(":latest") for i in images), images
    assert "registry.jithendar.com/jithendar/moks-terminal:latest" in images
    assert secrets == {("harbor-pull",)}
    assert not any("dkr.ecr" in str(doc) or "ecr-pull" in str(doc) for doc in docs)


def test_mk4_opts_into_the_terminal_with_https_only():
    """mk4 runs terminals: moks-terminal:latest from Harbor, one read-only profile, and egress to public HTTPS only."""
    docs = render("values-hetzner.yaml")
    assert not [d for d in docs if d["kind"] == "Namespace"]   # sessions run in moks-sre itself
    policy = of(docs, "NetworkPolicy", "terminals")["spec"]
    assert policy["podSelector"] == {"matchLabels": {"app": "moks-terminal"}}
    public = [r for r in policy["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]
    assert [r["ports"] for r in public] == [[{"port": 443, "protocol": "TCP"}]]
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"} <= set(public[0]["to"][0]["ipBlock"]["except"])


def test_the_gateway_is_routed_under_the_base_path_and_keeps_to_its_own_identity():
    docs = render("values-hetzner.yaml", *TERMINAL)
    terminal, portal = of(docs, "VirtualService", "moks")["spec"]["http"]
    # Both narrow the routing table's /moks prefix, so Istio's delegation accepts them.
    assert terminal["match"] == [{"uri": {"prefix": "/moks/api/terminal"}}] and terminal["route"][0]["destination"]["host"] == "moks-terminal.moks-sre.svc.cluster.local"
    assert portal["match"] == [{"uri": {"prefix": "/moks"}}] and portal["route"][0]["destination"]["host"] == "moks-api.moks-sre.svc.cluster.local"
    plain = render("values-hetzner.yaml", *TERMINAL, "istio.enabled=false", "ingress.enabled=true")
    paths = of(plain, "Ingress", "moks")["spec"]["rules"][0]["http"]["paths"]
    assert paths[0]["path"] == "/moks/api/terminal" and paths[0]["backend"]["service"]["name"] == "moks-terminal"
    assert paths[1]["path"] == "/moks" and paths[1]["backend"]["service"]["name"] == "moks-api"
    pod = dict(pods(docs))["moks-terminal"]
    assert pod["serviceAccountName"] == "moks-terminal" and secrets(pod) == {"moks-terminal-secrets"}
    assert not pod["containers"][0].get("volumeMounts", [])[1:]  # no credentials volume, no seed


def test_the_gateway_can_touch_only_the_terminal_namespace():
    docs = render("values-hetzner.yaml", *TERMINAL)
    assert not [d for d in docs if d["kind"] in ("ClusterRoleBinding", "RoleBinding") and any(s["name"] == "moks-terminal" for s in d.get("subjects", [])) and d["metadata"].get("namespace") != "moks-sre"]
    rules = of(docs, "Role", "moks-terminal-gateway")["rules"]
    assert {r for rule in rules for r in rule["resources"]} == {"pods", "pods/exec", "serviceaccounts/token"}
    assert next(r for r in rules if "serviceaccounts/token" in r["resources"])["resourceNames"] == ["moks-terminal-session"]


def test_the_session_identity_is_read_only_and_never_sees_secrets():
    docs = render("values-hetzner.yaml", *TERMINAL)
    rules = of(docs, "ClusterRole", "moks-terminal-reader")["rules"]
    assert {v for r in rules for v in r["verbs"]} == {"get", "list", "watch"}
    assert not {"secrets", "configmaps", "pods/exec", "pods/attach", "pods/portforward"} & {x for r in rules for x in r["resources"]}
    binding = of(docs, "RoleBinding", "moks-terminal-reader")
    assert binding["metadata"]["namespace"] == "moks-sre-lab" and binding["subjects"][0]["name"] == "moks-terminal-session"
    assert not [d for d in docs if d["kind"] == "ClusterRoleBinding" and "terminal" in d["metadata"]["name"]]


def test_sessions_share_the_release_namespace_closed_by_label_and_capped_by_one_quota():
    docs = render("values.yaml", *TERMINAL)
    assert not [d for d in docs if d["kind"] in ("Namespace", "LimitRange")]   # both would bind MOKS itself too
    for name, app in (("terminals", "moks-terminal"), ("diagnostics", "moks-diagnostic")):
        policy = of(docs, "NetworkPolicy", name)
        assert policy["metadata"]["namespace"] == "moks-sre" and policy["spec"]["podSelector"] == {"matchLabels": {"app": app}}
        assert policy["spec"]["ingress"] == [] and set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    quota = of(docs, "ResourceQuota", "moks-sessions")["spec"]
    assert quota["scopes"] == ["Terminating"] and set(quota["hard"]) == {"pods", "limits.cpu", "limits.memory"}
    assert not [d for d in docs if d["kind"] == "ResourceQuota" and d["metadata"]["name"] != "moks-sessions"]
    config = of(docs, "ConfigMap", "moks-config")["data"]
    assert config["MOKS_DIAGNOSTICS_NAMESPACE"] == "moks-sre"
    assert {"name": "MOKS_TERMINAL_NAMESPACE", "value": "moks-sre"} in dict(pods(docs))["moks-terminal"]["containers"][0]["env"]


# What the API server accepts in a quota with a Terminating/NotTerminating/BestEffort/NotBestEffort scope
# (ResourceQuota validation): anything else is "unsupported scope applied to resource" and fails the whole sync.
POD_SCOPE_RESOURCES = {"pods", "cpu", "memory", "requests.cpu", "requests.memory", "limits.cpu", "limits.memory"}


@pytest.mark.parametrize("values_file", ["values.yaml", "values-hetzner.yaml", "values-eks.yaml", "values-kind.yaml"])
def test_scoped_quotas_name_only_what_the_api_server_accepts(values_file):
    docs = render(values_file, *TERMINAL)
    scoped = [d for d in docs if d["kind"] == "ResourceQuota" and d["spec"].get("scopes")]
    assert scoped or values_file == "values-kind.yaml"
    for q in scoped:
        assert set(q["spec"]["hard"]) <= POD_SCOPE_RESOURCES, (q["metadata"]["name"], set(q["spec"]["hard"]) - POD_SCOPE_RESOURCES)


def test_jobs_never_count_against_the_session_quota():
    """Scope Terminating counts pods with activeDeadlineSeconds; MOKS's own Jobs set it on the Job only."""
    docs = render("values-hetzner.yaml", "backup.enabled=true", "backup.bucket=ci", "secrets.split=true", "ml.training.enabled=true", "ml.anomaly.namespaces=mk4:roboshop-ai")
    for d in docs:
        spec = d.get("spec") or {}
        pod = (spec.get("jobTemplate", {}).get("spec", {}).get("template") or spec.get("template") or {}).get("spec") or {}
        assert "activeDeadlineSeconds" not in pod, (d["kind"], d["metadata"]["name"])


def test_the_terminal_namespace_is_restricted_and_closed_when_it_has_its_own():
    docs = render("values.yaml", *TERMINAL, "terminal.namespace=moks-terminals")
    ns = of(docs, "Namespace", "moks-terminals")["metadata"]["labels"]
    assert ns["pod-security.kubernetes.io/enforce"] == "restricted"
    policy = of(docs, "NetworkPolicy", "terminals")["spec"]
    assert policy["podSelector"] == {} and policy["ingress"] == [] and set(policy["policyTypes"]) == {"Ingress", "Egress"}
    assert not [r for r in policy["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]  # nothing beyond DNS and the API server
    quota = of(docs, "ResourceQuota", "terminals")["spec"]["hard"]
    assert {"pods", "limits.cpu", "limits.memory", "limits.ephemeral-storage"} <= set(quota)


def test_configured_egress_is_exactly_what_was_listed():
    docs = render("values-hetzner.yaml", *TERMINAL, "terminal.network.egress[0].cidr=0.0.0.0/0", "terminal.network.egress[0].except={10.0.0.0/8,169.254.0.0/16}", "terminal.network.egress[0].ports={443}")
    rule = of(docs, "NetworkPolicy", "terminals")["spec"]["egress"][-1]
    assert rule["to"][0]["ipBlock"]["except"] == ["10.0.0.0/8", "169.254.0.0/16"] and rule["ports"] == [{"port": 443, "protocol": "TCP"}]


RERANKER = ("reranker.enabled=true", "reranker.image.digest=sha256:" + "b" * 64)


def test_reranking_is_absent_when_off():
    docs = render("values-eks.yaml", "image.tag=ci")
    assert not [d for d in docs if "reranker" in d["metadata"]["name"]]
    assert not [k for d in docs if d["kind"] == "ConfigMap" for k in (d.get("data") or {}) if "RERANK" in k]


def test_moks_knows_its_own_namespace_and_sandboxes():
    config = next(d for d in render("values-hetzner.yaml") if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]
    assert config["MOKS_OWN_NAMESPACE"] == "moks-sre" and config["MOKS_DIAGNOSTICS_NAMESPACE"] == "moks-sre"


def test_deploying_the_service_does_not_turn_it_on_for_search():
    config = next(d for d in render("values-hetzner.yaml", *RERANKER, "reranker.client.enabled=false") if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]
    assert config["MOKS_RAG_RERANK_ENABLED"] == "false" and config["MOKS_RAG_RERANK_REVISION"] and config["MOKS_RAG_RERANK_URL"].startswith("http://moks-reranker.moks-sre.svc")


def test_the_reranker_has_no_public_route_and_runs_restricted():
    docs = render("values-hetzner.yaml", *RERANKER)
    assert not [d for d in docs if d["kind"] in ("Ingress", "VirtualService") and "reranker" in str(d)]
    assert of(docs, "Service", "moks-reranker")["spec"]["type"] == "ClusterIP"
    pod = of(docs, "Deployment", "moks-reranker")["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"]["memory"] and "@sha256:" in c["image"] and c["imagePullPolicy"] == "IfNotPresent"
    assert {e["name"] for e in c["env"]} >= {"RERANKER_TOKEN", "RERANKER_THREADS"} and "envFrom" not in c  # never the database credentials


def test_only_the_api_and_worker_reach_it_and_it_reaches_nothing():
    docs = render("values-hetzner.yaml", *RERANKER)
    policy = of(docs, "NetworkPolicy", "moks-reranker")["spec"]
    assert policy["egress"] == [] and set(policy["policyTypes"]) == {"Ingress", "Egress"}
    callers = [p["podSelector"]["matchLabels"] for p in policy["ingress"][0]["from"]]
    assert callers == [{"app": "moks", "component": "api"}, {"app": "moks", "component": "worker"}]
    assert any(r.get("to") == [{"podSelector": {"matchLabels": {"app": "moks-reranker"}}}] for r in of(docs, "NetworkPolicy", "moks")["spec"]["egress"])


def test_a_tag_is_pulled_on_every_start_so_a_restart_gets_it():
    c = of(render("values-hetzner.yaml", "reranker.enabled=true", "reranker.image.tag=latest"), "Deployment", "moks-reranker")["spec"]["template"]["spec"]["containers"][0]
    assert c["image"].endswith("/moks-reranker:latest") and c["imagePullPolicy"] == "Always"


@pytest.mark.parametrize("bad", [("reranker.image.digest=latest",), ("reranker.image.tag=",)])
def test_an_unusable_image_reference_is_refused(bad):
    with pytest.raises(subprocess.CalledProcessError):
        render("values-hetzner.yaml", "reranker.enabled=true", *bad)


def test_the_mesh_allows_only_the_callers():
    docs = render("values-eks.yaml", *RERANKER)
    rule = of(docs, "AuthorizationPolicy", "moks-reranker")["spec"]["rules"][0]
    assert rule["to"][0]["operation"]["ports"] == ["8090"] and all(p.startswith("cluster.local/ns/moks-sre/sa/") for p in rule["from"][0]["source"]["principals"])


def test_mk4_seeds_bge_m3_and_lets_the_pods_reach_it():
    """The shared bge-m3 (miniKube deploy/minikube/embeddings/bge-m3) admits app=moks pods by NetworkPolicy and the
    moks service account by AuthorizationPolicy; this side needs the seed entry and the egress rule."""
    docs = render("values-hetzner.yaml")
    seed = yaml.safe_load(of(docs, "ConfigMap", "moks-seed")["data"]["seed.yaml"])
    (emb,) = [l for l in seed["llm"] if l["purpose"] == "embedding" and l["name"] == "bge-m3"]
    assert emb["endpoint"] == "http://bge-m3.embeddings:11434" and emb["model"] == "bge-m3:567m"
    assert {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "embeddings"}}}],
            "ports": [{"port": 11434, "protocol": "TCP"}]} in of(docs, "NetworkPolicy", "moks")["spec"]["egress"]
    by = dict(pods(docs))
    assert {by[n]["serviceAccountName"] for n in ("moks-api", "moks-worker", "moks-crewai")} == {"moks"}
    for n in ("moks-api", "moks-worker", "moks-crewai"):
        assert of(docs, "Deployment", n)["spec"]["template"]["metadata"]["labels"]["app"] == "moks"


def test_mk4_searches_postgresql_and_runs_no_search_pods():
    docs = render("values-hetzner.yaml")
    config = of(docs, "ConfigMap", "moks-config")["data"]
    assert config["MOKS_RAG_BACKEND"] == "postgres" and not [k for k in config if k.startswith("MOKS_OPENSEARCH")]
    assert not [d for d in docs if "opensearch" in d["metadata"]["name"] or d["metadata"]["name"].startswith("moks-search")]
    assert not [r for r in of(docs, "NetworkPolicy", "moks")["spec"]["egress"] if "opensearch" in str(r)]


# ---- secret sync -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("values", ["values.yaml", "values-eks.yaml"])
def test_secret_sync_is_off_by_default_and_adds_nothing(values):
    docs = render(values)
    assert not [d for d in docs if d["metadata"]["name"] == "moks-secret-sync"]
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")
    assert config["data"]["MOKS_SECRETS_SYNC_ENABLED"] == "false"


@pytest.mark.parametrize("split", [False, True])
def test_only_the_executor_holds_the_secret_sync_secret_and_secret_verbs(split):
    sets = ["secretsSync.enabled=true", "secretsSync.namespaces={shop,pay}", "secretsSync.allowDelete=false"] + (list(SPLIT) if split else [])
    docs = render("values-hetzner.yaml", *sets)
    holders = {name for name, pod in pods(docs) if "moks-secret-sync" in secrets(pod)}
    assert holders == {"moks-executor"}
    roles = {d["metadata"]["namespace"]: d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "moks-secret-sync"}
    assert set(roles) == {"shop", "pay"}
    verbs = next(r["verbs"] for r in roles["shop"]["rules"] if r["resources"] == ["secrets"])
    assert verbs == ["get", "list", "create", "update"]  # no delete unless allowDelete
    bindings = [d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "moks-secret-sync"]
    assert {s["name"] for b in bindings for s in b["subjects"]} == {"moks-executor"}
    reader = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "moks-reader")
    assert not [r for r in reader["rules"] if "secrets" in r["resources"]]  # the read-only connector still cannot read Secrets


@pytest.mark.parametrize("values", ["values-hetzner.yaml", "values-eks.yaml"])
def test_secret_sync_is_manual_only_unless_scheduling_is_chosen(values):
    def policy(*sets):
        docs = render(values, "secretsSync.enabled=true", "secretsSync.namespaces={shop}", *sets)
        return next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]["MOKS_SECRETS_SYNC_POLICY"]
    assert policy() == "manual"
    assert policy("secretsSync.policy=scheduled") == "scheduled"


def test_secret_delete_verb_needs_allow_delete():
    docs = render("values-hetzner.yaml", "secretsSync.enabled=true", "secretsSync.namespaces={shop}", "secretsSync.allowDelete=true")
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "moks-secret-sync")
    assert "delete" in next(r["verbs"] for r in role["rules"] if r["resources"] == ["secrets"])


def test_cnpg_status_is_readable_on_mk4_only_and_never_writable():
    def rule(values):
        reader = next(d for d in render(values) if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "moks-reader")
        return [r for r in reader["rules"] if r["apiGroups"] == ["postgresql.cnpg.io"]]
    assert rule("values-hetzner.yaml") == [{"apiGroups": ["postgresql.cnpg.io"], "resources": ["clusters", "backups", "scheduledbackups"], "verbs": ["get", "list"]}]
    assert rule("values-eks.yaml") == []  # off unless cnpg.enabled


def test_mk4_runs_the_embedding_service_beside_bge_m3_without_switching_search():
    """The benchmark's choice (docs/EMBEDDINGS.md) is deployed and seeded inactive: bge-m3 keeps serving until the new
    model passes its evaluation and an index is rebuilt with it."""
    docs = render("values-hetzner.yaml")
    seed = yaml.safe_load(of(docs, "ConfigMap", "moks-seed")["data"]["seed.yaml"])
    by_name = {l["name"]: l for l in seed["llm"] if l["purpose"] == "embedding"}
    assert by_name["bge-m3"]["is_active"] is True
    new = by_name["bge-base-en-v1-5"]
    assert new["is_active"] is False and new["provider"] == "huggingface" and new["endpoint"] == "http://moks-embedder:8090"
    assert new["embedding_options"]["query_prefix"].startswith("Represent this sentence") and new["embedding_options"]["revision"]
    lock = json.loads((CHART.parents[2] / "reranker" / "embedder.lock.json").read_text())
    assert (new["model"], new["embedding_options"]["revision"]) == (lock["model_id"], lock["revision"])
    assert new["embedding_options"]["query_prefix"] == lock["query_prefix"]
    pod = dict(pods(docs))["moks-embedder"]
    (c,) = pod["containers"]
    env = {e["name"]: e for e in c["env"]}
    assert env["RERANKER_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "moks-reranker-secrets", "key": "MOKS_CRED_RERANKER_TOKEN"}
    assert new["credential_ref"] == {"type": "env", "name": "MOKS_CRED_RERANKER_TOKEN"}
    assert pod["automountServiceAccountToken"] is False and c["resources"]["limits"]["memory"]
    policy = of(docs, "NetworkPolicy", "moks-embedder")["spec"]
    assert policy["egress"] == [] and {"podSelector": {"matchLabels": {"app": "moks", "component": "api"}}} in policy["ingress"][0]["from"]
    assert {"to": [{"podSelector": {"matchLabels": {"app": "moks-embedder"}}}], "ports": [{"port": 8090, "protocol": "TCP"}]} in of(docs, "NetworkPolicy", "moks")["spec"]["egress"]


def test_the_embedding_service_is_off_by_default():
    assert "moks-embedder" not in dict(pods(render("values.yaml")))


SHARED_READERS = ("moks-api", "moks-worker", "moks-crewai")


def test_shared_knowledge_reads_its_url_and_token_from_a_secret_and_reaches_only_rag_api():
    """docs/SHARED_KNOWLEDGE.md: RAG_API_URL and RAG_BEARER_TOKEN by secretKeyRef (optional: pods start without the Secret),
    RAG_COLLECTION from moks-config, egress to rag:8000. Only the pods that search or feed moks-rag get the token."""
    docs = render("values-hetzner.yaml")
    by = dict(pods(docs))
    for name in SHARED_READERS:
        env = {e["name"]: e for c in by[name]["containers"] for e in c.get("env", [])}
        for var in ("RAG_API_URL", "RAG_BEARER_TOKEN"):
            assert env[var]["valueFrom"]["secretKeyRef"] == {"name": "moks-rag-client", "key": var, "optional": True}, (name, var)
    for name, pod in by.items():
        if name not in SHARED_READERS:
            assert "moks-rag-client" not in secrets(pod), name  # never the executor, the terminal, backups or migrations
    assert of(docs, "ConfigMap", "moks-config")["data"]["RAG_COLLECTION"] == "sre_knowledge"
    rag = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "rag"}}}], "ports": [{"port": 8000, "protocol": "TCP"}]}
    assert rag in of(docs, "NetworkPolicy", "moks")["spec"]["egress"]
    named = render("values-hetzner.yaml", "sharedKnowledge.secret=rag-client", "sharedKnowledge.collection=sre_knowledge_v2")
    assert of(named, "ConfigMap", "moks-config")["data"]["RAG_COLLECTION"] == "sre_knowledge_v2"
    assert "rag-client" in secrets(dict(pods(named))["moks-api"])


def test_shared_knowledge_is_off_unless_enabled():
    docs = render("values-eks.yaml")
    assert not any(e["name"].startswith("RAG_") for _, p in pods(docs) for c in p["containers"] for e in c.get("env", []))
    assert "RAG_COLLECTION" not in of(docs, "ConfigMap", "moks-config")["data"]

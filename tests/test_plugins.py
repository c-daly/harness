"""Plugin loader: manifests, discovery, load-time validation."""

import pytest

from harness.plugins import PluginError, load_plugins

MINIMAL_MANIFEST = """
[plugin]
name = "demo"
version = "0.1.0"
description = "A demo plugin"
"""


def write_plugin(root, name="demo", manifest=MINIMAL_MANIFEST, files=None):
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(manifest)
    for rel, content in (files or {}).items():
        path = plugin_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return plugin_dir


def test_minimal_plugin_loads(tmp_path):
    write_plugin(tmp_path)
    loaded = load_plugins([tmp_path])
    assert [p.name for p in loaded.plugins] == ["demo"]
    assert loaded.warnings == []


def test_dirs_without_plugin_toml_are_skipped(tmp_path):
    (tmp_path / "not-a-plugin").mkdir()
    loaded = load_plugins([tmp_path])
    assert loaded.plugins == []


@pytest.mark.parametrize(
    "manifest,fragment",
    [
        ("[plugin]\nversion='1'\ndescription='d'\n", "name"),
        ("[plugin]\nname='bad name'\nversion='1'\ndescription='d'\n", "name"),
        ("[plugin]\nname='a__b'\nversion='1'\ndescription='d'\n", "name"),
        ("[plugin]\nname='x'\ndescription='d'\n", "version"),
        ("[plugin]\nname='x'\nversion='1'\n", "description"),
        ("not toml [", "TOML"),
        ("[plugin]\nname='x'\nversion='1'\ndescription='d'\nbogus='y'\n", "unknown"),
    ],
)
def test_manifest_validation_errors(tmp_path, manifest, fragment):
    write_plugin(tmp_path, manifest=manifest)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert fragment.lower() in str(exc.value).lower()


def test_duplicate_plugin_names_across_dirs_fail(tmp_path):
    write_plugin(tmp_path / "a")
    write_plugin(tmp_path / "b")
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path / "a", tmp_path / "b"])
    assert "demo" in str(exc.value)


def test_depends_must_be_loaded(tmp_path):
    manifest = MINIMAL_MANIFEST.replace(
        'description = "A demo plugin"', 'description = "d"\ndepends = ["missing"]'
    )
    write_plugin(tmp_path, manifest=manifest)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "missing" in str(exc.value)


def test_depends_satisfied_loads_in_order(tmp_path):
    base_manifest = MINIMAL_MANIFEST.replace('name = "demo"', 'name = "base"')
    write_plugin(tmp_path, name="base", manifest=base_manifest)
    dep_manifest = MINIMAL_MANIFEST.replace(
        'description = "A demo plugin"', 'description = "d"\ndepends = ["base"]'
    )
    write_plugin(tmp_path, name="demo", manifest=dep_manifest)
    loaded = load_plugins([tmp_path])
    names = [p.name for p in loaded.plugins]
    assert names.index("base") < names.index("demo")


def test_dependency_cycle_is_loud(tmp_path):
    m1 = "[plugin]\nname='p1'\nversion='1'\ndescription='d'\ndepends=['p2']\n"
    m2 = "[plugin]\nname='p2'\nversion='1'\ndescription='d'\ndepends=['p1']\n"
    write_plugin(tmp_path, name="p1", manifest=m1)
    write_plugin(tmp_path, name="p2", manifest=m2)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "cycle" in str(exc.value)


def test_skills_commands_agents_discovered(tmp_path):
    write_plugin(
        tmp_path,
        files={
            "skills/remembering.md": "---\nname: remembering\ndescription: d\n---\nbody",
            "commands/brief.md": "---\nname: brief\ndescription: d\n---\nShow $ARGUMENTS",
            "agents/curator.md": "---\nname: curator\ndescription: d\n---\nYou curate.",
        },
    )
    loaded = load_plugins([tmp_path])
    assert [s.name for s in loaded.skills] == ["remembering"]
    assert [c.name for c in loaded.commands] == ["brief"]
    assert [a.name for a in loaded.agents] == ["curator"]


def test_bad_skill_frontmatter_fails_the_plugin_load(tmp_path):
    write_plugin(tmp_path, files={"skills/bad.md": "no frontmatter"})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "bad.md" in str(exc.value)


def test_cross_plugin_skill_name_collision_fails(tmp_path):
    skill = "---\nname: same\ndescription: d\n---\nbody"
    write_plugin(
        tmp_path,
        name="p1",
        manifest=MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p1"'),
        files={"skills/same.md": skill},
    )
    write_plugin(
        tmp_path,
        name="p2",
        manifest=MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p2"'),
        files={"skills/same.md": skill},
    )
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "same" in str(exc.value)


def test_emitter_namespaces_validated(tmp_path):
    for bad, fragment in [
        ("harness", "reserved"),
        ("mcp", "reserved"),
        ("plugin", "reserved"),
        ("bad ns", "namespace"),
        ("a__b", "namespace"),
    ]:
        manifest = MINIMAL_MANIFEST + f'\n[emitters]\nnamespaces = ["{bad}"]\n'
        root = tmp_path / f"case-{bad.replace(' ', '_')}"
        write_plugin(root, manifest=manifest)
        with pytest.raises(PluginError) as exc:
            load_plugins([root])
        assert fragment in str(exc.value).lower()


def test_emitter_namespace_cross_plugin_collision(tmp_path):
    m1 = MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p1"') + (
        '\n[emitters]\nnamespaces = ["mem"]\n'
    )
    m2 = MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p2"') + (
        '\n[emitters]\nnamespaces = ["mem"]\n'
    )
    write_plugin(tmp_path, name="p1", manifest=m1)
    write_plugin(tmp_path, name="p2", manifest=m2)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "mem" in str(exc.value)


def test_mcp_server_spec_with_plugin_root_substitution(tmp_path):
    manifest = MINIMAL_MANIFEST + (
        '\n[mcp.servers.demo-server]\ncommand = "python3"\nargs = ["${PLUGIN_ROOT}/server.py"]\n'
    )
    plugin_dir = write_plugin(tmp_path, manifest=manifest)
    loaded = load_plugins([tmp_path])
    (spec,) = loaded.mcp_servers
    assert spec.source == "plugin"
    assert spec.args == (f"{plugin_dir}/server.py",)


def test_dispatch_hook_priority_validated(tmp_path):
    manifest = MINIMAL_MANIFEST + (
        '\n[hooks]\nmodule = "hooks.py"\n'
        '[[hooks.dispatch]]\nname = "g"\nfunction = "g"\npriority = 1000\n'
    )
    write_plugin(tmp_path, manifest=manifest, files={"hooks.py": "def g(a):\n    return None\n"})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "priority" in str(exc.value)


def test_lifecycle_hook_on_unfired_point_warns(tmp_path):
    manifest = MINIMAL_MANIFEST + (
        '\n[hooks]\nmodule = "hooks.py"\n'
        '[[hooks.lifecycle]]\nname = "h"\nfunction = "h"\npoint = "post_tool"\n'
    )
    write_plugin(tmp_path, manifest=manifest, files={"hooks.py": "def h(ctx):\n    return []\n"})
    loaded = load_plugins([tmp_path])
    assert any("post_tool" in w and "never fire" in w for w in loaded.warnings)


def test_lifecycle_hook_invalid_point_is_loud(tmp_path):
    manifest = MINIMAL_MANIFEST + (
        '\n[hooks]\nmodule = "hooks.py"\n'
        '[[hooks.lifecycle]]\nname = "h"\nfunction = "h"\npoint = "nope"\n'
    )
    write_plugin(tmp_path, manifest=manifest, files={"hooks.py": "def h(ctx):\n    return []\n"})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "nope" in str(exc.value)


@pytest.mark.parametrize(
    "manifest,fragment",
    [
        # top-level scalars must come BEFORE [plugin] or TOML scopes them inside it
        ("mcp = 42\n" + MINIMAL_MANIFEST, "[mcp] must be a table"),
        ("emitters = [1]\n" + MINIMAL_MANIFEST, "[emitters] must be a table"),
        ("subscribers = 42\n" + MINIMAL_MANIFEST, "array of tables"),
        (MINIMAL_MANIFEST + '[hooks]\nmodule = "h.py"\ndispatch = 42\n', "array of tables"),
        (MINIMAL_MANIFEST + '[hooks]\nmodule = "h.py"\nlifecycle = 42\n', "array of tables"),
        (MINIMAL_MANIFEST + '[hooks]\nmodule = "h.py"\nbogus = 1\n', "unknown [hooks] keys"),
    ],
)
def test_wrong_typed_sections_are_plugin_errors(tmp_path, manifest, fragment):
    write_plugin(tmp_path, manifest=manifest)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert fragment in str(exc.value)


def test_hooks_declared_without_module_is_loud(tmp_path):
    manifest = MINIMAL_MANIFEST + ('\n[[hooks.dispatch]]\nname = "g"\nfunction = "g"\n')
    write_plugin(tmp_path, manifest=manifest)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "module" in str(exc.value)


# ---------------------------------------------------------------------------
# Task 3: hook-module loading -- callables validated at load, never broken at runtime
# ---------------------------------------------------------------------------

HOOKS_MANIFEST = (
    MINIMAL_MANIFEST
    + """
[hooks]
module = "hooks.py"

[[hooks.dispatch]]
name = "guard"
function = "guard"

[[hooks.lifecycle]]
name = "brief"
function = "session_brief"
point = "session_start"

[[subscribers]]
name = "audit"
module = "hooks.py"
function = "audit"
"""
)

HOOKS_PY = """
from harness.hooks import Allow, Inject


def guard(action):
    return Allow()


def session_brief(ctx):
    return [Inject(text="brief!")]


async def audit(envelope):
    pass
"""


def test_hook_module_loads_and_resolves_callables(tmp_path):
    write_plugin(tmp_path, manifest=HOOKS_MANIFEST, files={"hooks.py": HOOKS_PY})
    loaded = load_plugins([tmp_path])
    (plugin,) = loaded.plugins
    assert callable(plugin.dispatch_callables["guard"])
    assert callable(plugin.lifecycle_callables["brief"])
    assert callable(plugin.subscriber_callables["audit"])


def test_hook_module_import_error_fails_load(tmp_path):
    write_plugin(
        tmp_path, manifest=HOOKS_MANIFEST, files={"hooks.py": "import nonexistent_module_xyz\n"}
    )
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "hooks.py" in str(exc.value)


def test_missing_function_fails_load(tmp_path):
    write_plugin(
        tmp_path,
        manifest=HOOKS_MANIFEST,
        files={"hooks.py": "def guard(action):\n    return None\n"},
    )
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "session_brief" in str(exc.value)


def test_non_callable_attribute_fails_load(tmp_path):
    bad = HOOKS_PY + "\nsession_brief = 42\n"
    write_plugin(tmp_path, manifest=HOOKS_MANIFEST, files={"hooks.py": bad})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "callable" in str(exc.value)


def test_two_plugins_same_module_filename_are_isolated(tmp_path):
    # both plugins ship hooks.py -- importlib must not cross-wire them
    p1 = MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p1"') + (
        """
[hooks]
module = "hooks.py"
[[hooks.dispatch]]
name = "g"
function = "g"
"""
    )
    p2 = MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p2"') + (
        """
[hooks]
module = "hooks.py"
[[hooks.dispatch]]
name = "g"
function = "g"
"""
    )
    write_plugin(
        tmp_path,
        name="p1",
        manifest=p1,
        files={
            "hooks.py": """MARK='p1'
def g(a):
    return MARK
"""
        },
    )
    write_plugin(
        tmp_path,
        name="p2",
        manifest=p2,
        files={
            "hooks.py": """MARK='p2'
def g(a):
    return MARK
"""
        },
    )
    loaded = load_plugins([tmp_path])
    fns = {p.name: p.dispatch_callables["g"] for p in loaded.plugins}
    assert fns["p1"](None) == "p1"
    assert fns["p2"](None) == "p2"


def test_subscriber_must_be_async(tmp_path):
    # A sync subscriber function must be rejected at load time
    sync_subscriber_py = """
from harness.hooks import Allow, Inject


def guard(action):
    return Allow()


def session_brief(ctx):
    return [Inject(text="brief!")]


def audit(envelope):
    pass
"""
    write_plugin(tmp_path, manifest=HOOKS_MANIFEST, files={"hooks.py": sync_subscriber_py})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "async" in str(exc.value)


def test_multi_plugin_failure_leaves_no_module_residue(tmp_path):
    import sys

    before = {k for k in sys.modules if k.startswith("harness_plugin_")}
    ok_manifest = MINIMAL_MANIFEST.replace('name = "demo"', 'name = "alpha"') + (
        '\n[hooks]\nmodule = "hooks.py"\n[[hooks.dispatch]]\nname = "g"\nfunction = "g"\n'
    )
    write_plugin(
        tmp_path,
        name="alpha",
        manifest=ok_manifest,
        files={"hooks.py": "def g(a):\n    return None\n"},
    )
    bad_manifest = MINIMAL_MANIFEST.replace('name = "demo"', 'name = "beta"') + (
        '\n[hooks]\nmodule = "hooks.py"\n[[hooks.dispatch]]\nname = "g"\nfunction = "g"\n'
    )
    write_plugin(
        tmp_path,
        name="beta",
        manifest=bad_manifest,
        files={"hooks.py": "import nonexistent_module_xyz\n"},
    )
    with pytest.raises(PluginError):
        load_plugins([tmp_path])
    after = {k for k in sys.modules if k.startswith("harness_plugin_")}
    assert after == before


# ---------------------------------------------------------------------------
# Task 6: apply_plugins / build_kernel(plugins=) wiring
# ---------------------------------------------------------------------------

FULL_MANIFEST = MINIMAL_MANIFEST + (
    '\n[hooks]\nmodule = "hooks.py"\n\n[[hooks.dispatch]]\nname = "guard"\nfunction = "guard"\n\n[[hooks.lifecycle]]\nname = "brief"\nfunction = "session_brief"\npoint = "session_start"\n\n[[subscribers]]\nname = "audit"\nmodule = "hooks.py"\nfunction = "audit"\n\n[emitters]\nnamespaces = ["myplugin"]\n'
)

FULL_HOOKS_PY = 'from harness.hooks import Allow, Inject\n\n\n_SEEN = []\n\n\ndef guard(action):\n    return Allow()\n\n\ndef session_brief(ctx):\n    return [Inject(text="brief!")]\n\n\nasync def audit(envelope):\n    _SEEN.append(envelope.event.type)\n'


async def test_kernel_applies_plugin_primitives(tmp_path):
    from harness.cli import build_kernel, run_once
    from harness.log import read_session
    from harness.provider import EchoProvider
    from harness.types import ModelId

    plugin_root = tmp_path / "plugins"
    write_plugin(
        plugin_root,
        manifest=FULL_MANIFEST,
        files={
            "hooks.py": FULL_HOOKS_PY,
            "skills/hello.md": "---\nname: hello\ndescription: A greeting skill\n---\nHello world!",
        },
    )
    loaded = load_plugins([plugin_root])
    kernel = build_kernel(
        provider=EchoProvider(),
        base_dir=tmp_path / "base",
        model=ModelId("echo"),
        plugins=loaded,
    )
    names = {str(s.name) for s in kernel.registry.specs()}
    assert "invoke_skill" in names
    reply = await run_once(kernel, "hi")
    assert reply == "echo: hi"
    assert "brief!" in kernel.loop.system_prompt
    events = [e.event for e in read_session(tmp_path / "base", kernel.session.id)]
    types = [e.type for e in events]
    assert types[0] == "session_started"
    customs = [e for e in events if e.type == "custom"]
    assert any(c.namespace == "plugin" and c.name == "plugin_loaded" for c in customs)


async def test_kernel_no_plugins_backward_compatible(tmp_path):
    from harness.cli import build_kernel, run_once
    from harness.provider import EchoProvider
    from harness.types import ModelId

    kernel = build_kernel(
        provider=EchoProvider(),
        base_dir=tmp_path,
        model=ModelId("echo"),
    )
    names = {str(s.name) for s in kernel.registry.specs()}
    assert "invoke_skill" not in names
    reply = await run_once(kernel, "hi")
    assert reply == "echo: hi"


def test_apply_plugins_tool_collision_raises(tmp_path):
    from harness.hooks import HookBus
    from harness.plugins import PluginError, apply_plugins
    from harness.skills import InvokeSkillTool, SkillSet
    from harness.tools import ToolRegistry

    plugin_root = tmp_path / "plugins"
    write_plugin(
        plugin_root,
        manifest=MINIMAL_MANIFEST,
        files={
            "skills/hello.md": "---\nname: hello\ndescription: d\n---\nbody",
        },
    )
    loaded = load_plugins([plugin_root])
    registry = ToolRegistry()
    hooks = HookBus()
    registry.register(InvokeSkillTool(SkillSet([])))
    with pytest.raises(PluginError, match="invoke_skill"):
        apply_plugins(loaded, registry=registry, hooks=hooks, agents_sink={})


async def test_plugin_agents_feed_subagent_runner(tmp_path):
    from harness.cli import build_kernel
    from harness.provider import EchoProvider
    from harness.types import ModelId

    plugin_root = tmp_path / "plugins"
    write_plugin(
        plugin_root,
        manifest=MINIMAL_MANIFEST,
        files={
            "agents/curator.md": "---\nname: curator\ndescription: curates\n---\nYou curate.",
        },
    )
    loaded = load_plugins([plugin_root])
    kernel = build_kernel(
        provider=EchoProvider(),
        base_dir=tmp_path / "base",
        model=ModelId("echo"),
        plugins=loaded,
    )
    specs = {str(s.name): s for s in kernel.registry.specs()}
    assert "curator" in specs["dispatch_agent"].description


async def test_plugin_dispatch_hook_registered(tmp_path):
    from harness.cli import build_kernel, run_once
    from harness.events import HookDecided
    from harness.log import read_session
    from harness.provider import EchoProvider
    from harness.types import ModelId

    plugin_root = tmp_path / "plugins"
    write_plugin(
        plugin_root,
        manifest=FULL_MANIFEST,
        files={
            "hooks.py": FULL_HOOKS_PY,
        },
    )
    loaded = load_plugins([plugin_root])
    kernel = build_kernel(
        provider=EchoProvider(),
        base_dir=tmp_path / "base",
        model=ModelId("echo"),
        plugins=loaded,
    )
    await run_once(kernel, "hi")
    events = [e.event for e in read_session(tmp_path / "base", kernel.session.id)]
    hook_names = [e.hook for e in events if isinstance(e, HookDecided)]
    assert any("plugin:demo:guard" in name for name in hook_names)

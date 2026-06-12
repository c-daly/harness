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
    m1 = (
        "[plugin]\nname='p1'\nversion='1'\ndescription='d'\ndepends=['p2']\n"
    )
    m2 = (
        "[plugin]\nname='p2'\nversion='1'\ndescription='d'\ndepends=['p1']\n"
    )
    write_plugin(tmp_path, name="p1", manifest=m1)
    write_plugin(tmp_path, name="p2", manifest=m2)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "cycle" in str(exc.value)


def test_skills_commands_agents_discovered(tmp_path):
    write_plugin(tmp_path, files={
        "skills/remembering.md": "---\nname: remembering\ndescription: d\n---\nbody",
        "commands/brief.md": "---\nname: brief\ndescription: d\n---\nShow $ARGUMENTS",
        "agents/curator.md": "---\nname: curator\ndescription: d\n---\nYou curate.",
    })
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
    write_plugin(tmp_path, name="p1",
                 manifest=MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p1"'),
                 files={"skills/same.md": skill})
    write_plugin(tmp_path, name="p2",
                 manifest=MINIMAL_MANIFEST.replace('name = "demo"', 'name = "p2"'),
                 files={"skills/same.md": skill})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "same" in str(exc.value)


def test_emitter_namespaces_validated(tmp_path):
    for bad, fragment in [
        ("harness", "reserved"), ("mcp", "reserved"), ("plugin", "reserved"),
        ("bad ns", "namespace"), ("a__b", "namespace"),
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
        '\n[mcp.servers.demo-server]\ncommand = "python3"\n'
        'args = ["${PLUGIN_ROOT}/server.py"]\n'
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
    write_plugin(tmp_path, manifest=manifest,
                 files={"hooks.py": "def g(a):\n    return None\n"})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "priority" in str(exc.value)


def test_lifecycle_hook_on_unfired_point_warns(tmp_path):
    manifest = MINIMAL_MANIFEST + (
        '\n[hooks]\nmodule = "hooks.py"\n'
        '[[hooks.lifecycle]]\nname = "h"\nfunction = "h"\npoint = "post_tool"\n'
    )
    write_plugin(tmp_path, manifest=manifest,
                 files={"hooks.py": "def h(ctx):\n    return []\n"})
    loaded = load_plugins([tmp_path])
    assert any("post_tool" in w and "never fire" in w for w in loaded.warnings)


def test_lifecycle_hook_invalid_point_is_loud(tmp_path):
    manifest = MINIMAL_MANIFEST + (
        '\n[hooks]\nmodule = "hooks.py"\n'
        '[[hooks.lifecycle]]\nname = "h"\nfunction = "h"\npoint = "nope"\n'
    )
    write_plugin(tmp_path, manifest=manifest,
                 files={"hooks.py": "def h(ctx):\n    return []\n"})
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "nope" in str(exc.value)


@pytest.mark.parametrize(
    "extra,fragment",
    [
        ("mcp = 42\n", "[mcp] must be a table"),
        ("emitters = [1]\n", "[emitters] must be a table"),
        ("subscribers = 42\n", "array of tables"),
        ('[hooks]\nmodule = "h.py"\ndispatch = 42\n', "array of tables"),
        ('[hooks]\nmodule = "h.py"\nlifecycle = 42\n', "array of tables"),
        ('[hooks]\nmodule = "h.py"\nbogus = 1\n', "unknown [hooks] keys"),
    ],
)
def test_wrong_typed_sections_are_plugin_errors(tmp_path, extra, fragment):
    write_plugin(tmp_path, manifest=MINIMAL_MANIFEST + extra)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert fragment in str(exc.value)


def test_hooks_declared_without_module_is_loud(tmp_path):
    manifest = MINIMAL_MANIFEST + (
        '\n[[hooks.dispatch]]\nname = "g"\nfunction = "g"\n'
    )
    write_plugin(tmp_path, manifest=manifest)
    with pytest.raises(PluginError) as exc:
        load_plugins([tmp_path])
    assert "module" in str(exc.value)

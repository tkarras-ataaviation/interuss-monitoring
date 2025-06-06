import base64
import json
import os
import re
from typing import Dict, List, Optional, Tuple, Union

import _jsonnet
import bc_jsonpath_ng
import requests
import yaml
from loguru import logger

FILE_PREFIX = "file://"
HTTP_PREFIX = "http://"
HTTPS_PREFIX = "https://"
RECOGNIZED_EXTENSIONS = ".json", ".yaml", ".kml", ".jsonnet"


FileReference = str
"""Location of a file containing content.

May be:
  * file://<PATH>
  * http(s)://<PATH>
  * Python-package style name relative to the uss_qualifier package (without extension; extension will be inferred by what file is present)

Allowed extensions:
  * .json (dict, content)
  * .yaml (dict, content)
  * .jsonnet (dict, content)
  * .kml (content)
"""

_package_root = os.path.dirname(__file__)


def resolve_filename(data_file: FileReference) -> str:
    if data_file.startswith(FILE_PREFIX):
        # file:// explicit local file reference
        return os.path.abspath(data_file[len(FILE_PREFIX) :])
    elif data_file.startswith(HTTP_PREFIX) or data_file.startswith(HTTPS_PREFIX):
        # http(s):// web file reference
        return data_file
    else:
        # Package-based name (without extension)
        path_parts = [_package_root]
        path_parts += data_file.split(".")
        file_name = None

        for ext in RECOGNIZED_EXTENSIONS:
            ext_file = os.path.join(*path_parts) + ext
            if os.path.exists(ext_file):
                return os.path.abspath(ext_file)

        if file_name is None:
            raise NotImplementedError(
                f"Cannot find a suitable file to load for {data_file}"
            )


def get_package_name(local_file_path: str) -> FileReference:
    """Get the Python-package style name of the specified file path on the local system.

    Args:
        local_file_path: Path to YAML or JSON file on the local system.

    Returns: Python-package style name; e.g., suites.astm.netrid.f3411_19
    """
    base, ext = os.path.splitext(local_file_path)
    if ext.lower() not in {".yaml", ".json"}:
        raise ValueError(
            f"Package name does not exist for non-dictionary file {local_file_path}"
        )
    rel_path = os.path.relpath(base, start=_package_root)
    return ".".join(os.path.normpath(rel_path).split(os.path.sep))


def _get_web_content(url: str) -> str:
    headers = {}

    # Check if this is a request to a private GitHub repo
    github_private_repos_key = "GITHUB_PRIVATE_REPOS"
    if os.environ.get(github_private_repos_key):
        github_match = re.match(
            r"^https://(?P<hostname>github\.com|raw\.githubusercontent\.com|api\.github\.com)/(?P<org>[^/]*)/(?P<repo>[^/?#]*)(?P<predicate>.*)$",
            url,
        )
        if github_match:
            if github_match.group("hostname") == "github.com":
                logger.warning(
                    f"{url} references the main GitHub UI; did you mean to specify a reference to the corresponding content on raw.githubusercontent.com?"
                )
            org = github_match.group("org")
            repo = github_match.group("repo")

            # Extract personal access token(s) and applicability from environment variable
            token = None
            pat_defs = os.environ.get(github_private_repos_key).split(";")
            for pat_def in pat_defs:
                patdef_match = re.match(
                    f"^(?P<org>[^/]*)/(?P<repos>[^:]*):(?P<token>.*)$", pat_def
                )
                if not patdef_match:
                    raise ValueError(
                        f"Error in {github_private_repos_key} environment variable: element `{pat_def}` does not follow the pattern ORG/REPOS:TOKEN"
                    )
                token_org = patdef_match.group("org")
                token_repos = patdef_match.group("repos").split(",")
                if org == token_org and repo in token_repos:
                    token = patdef_match.group("token")
                    break

            if token is not None:
                # This request is for a resource in a private GitHub repo that we have a personal access token for.
                headers["Authorization"] = (
                    f"Basic {base64.b64encode(token.encode()).decode()}"
                )

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.content.decode("utf-8")


def _load_content_from_file_name(file_name: str) -> str:
    if file_name.startswith(HTTP_PREFIX) or file_name.startswith(HTTPS_PREFIX):
        # http(s):// web file reference
        file_content = _get_web_content(file_name)
    else:
        with open(file_name, "r") as f:
            file_content = f.read()

    return file_content


def load_content(data_file: FileReference) -> str:
    return _load_content_from_file_name(resolve_filename(data_file))


def _split_anchor(file_name: str) -> Tuple[str, Optional[str]]:
    if "#" in file_name:
        anchor_location = file_name.index("#")
        base_file_name = file_name[0:anchor_location]
        anchor = file_name[anchor_location + 1 :]
    else:
        base_file_name = file_name
        anchor = None
    return base_file_name, anchor


def load_dict_with_references(data_file: FileReference) -> dict:
    """Loads a dict from the specified file reference.

    If the data_file has a #<COMPONENT_PATH> suffix, the component at that path
    will be selected.  For example, #/foo/bar will select content["foo"]["bar"].

    If any key at any level of the loaded content is "$ref", then the value is
    expected to be a string that refers to a file (plus, optionally, a component
    path following #), or a blank string followed by a # component path which
    refers to a component within the current file.  All of the keys from the
    $ref will be added to the parent object of the $ref, and the $ref will be
    removed.  This $ref convention is generally compatible with OpenAPI, except
    that other keys may co-exist with $ref.  Multiple $refs may be used when
    enclosed in an allOf key (with an array as a value), again similar to
    OpenAPI.
    """
    base_file_name, anchor = _split_anchor(data_file)
    base_file_name = resolve_filename(base_file_name)
    file_name = base_file_name + (f"#{anchor}" if anchor is not None else "")
    dict_content, _ = _load_dict_with_references_from_file_name(file_name, file_name)
    return dict_content


def _jsonnet_import_callback(
    base_file_name: str, folder: str, rel: str, cache: Optional[Dict[str, dict]]
) -> Tuple[str, bytes]:
    if rel.endswith(".libsonnet"):
        # Do not attempt to parse libsonnet content (e.g., resolve $refs);
        # it will be parsed after loading the full top-level Jsonnet.
        file_name = os.path.join(folder, rel)
        file_content = _load_content_from_file_name(file_name)
        return file_name, file_content.encode()
    else:
        dict_content, file_name = _load_dict_with_references_from_file_name(
            rel, base_file_name, cache
        )
        return file_name, json.dumps(dict_content).encode()


def _load_dict_with_references_from_file_name(
    file_name: str, context_file_name: str, cache: Optional[Dict[str, dict]] = None
) -> Tuple[dict, str]:
    if cache is None:
        cache = {}

    base_file_name, anchor = _split_anchor(file_name)

    if base_file_name.startswith(FILE_PREFIX):
        base_file_name = base_file_name[len(FILE_PREFIX) :]
    if (
        not base_file_name.startswith(HTTP_PREFIX)
        and not base_file_name.startswith(HTTPS_PREFIX)
        and not base_file_name.startswith("/")
    ):
        if (
            base_file_name.startswith(".")
            or "/" in base_file_name
            or "\\" in base_file_name
            or base_file_name.lower().endswith(".yaml")
            or base_file_name.lower().endswith(".json")
            or base_file_name.lower().endswith(".jsonnet")
        ):
            # This is a relative file path; it should be relative to the context
            root_path = os.path.dirname(context_file_name)
            base_file_name = os.path.join(root_path, base_file_name)
        else:
            # This is a package-based file path
            base_file_name = resolve_filename(base_file_name)

    if not base_file_name.startswith(HTTP_PREFIX) and not base_file_name.startswith(
        HTTPS_PREFIX
    ):
        base_file_name = os.path.abspath(base_file_name)

    if base_file_name in cache:
        dict_content = cache[base_file_name]
    else:
        file_content = _load_content_from_file_name(base_file_name)

        if base_file_name.lower().endswith(".json"):
            dict_content = json.loads(file_content)
        elif base_file_name.lower().endswith(".yaml"):
            dict_content = yaml.safe_load(file_content)
        elif base_file_name.lower().endswith(".jsonnet"):

            def import_callback(folder: str, rel: str):
                return _jsonnet_import_callback(base_file_name, folder, rel, cache)

            json_str = _jsonnet.evaluate_snippet(
                base_file_name, file_content, import_callback=import_callback
            )
            dict_content = json.loads(json_str)
        else:
            raise NotImplementedError(
                f'Unable to parse data for "{base_file_name}" because its extension-based data format is not supported'
            )

        allof_paths = _identify_allofs(dict_content)
        ref_paths = _identify_refs(dict_content)
        _replace_refs(dict_content, base_file_name, ref_paths, allof_paths, cache)
        cache[base_file_name] = dict_content

    if anchor is not None:
        return _select_path(dict_content, anchor), base_file_name
    else:
        return dict_content, base_file_name


def _should_recurse(item):
    if isinstance(item, dict):
        return True
    if isinstance(item, str):
        return False
    try:
        iterable = iter(item)
    except TypeError:
        iterable = None
    if iterable is not None:
        return True
    return False


def _is_descendant(potential_descendant: str, ancestor: str) -> bool:
    ancestor_paths = ancestor.split(".")
    potential_descendant_paths = potential_descendant.split(".")
    if len(potential_descendant_paths) < len(ancestor_paths):
        return False
    result = potential_descendant_paths[0 : len(ancestor_paths)] == ancestor_paths
    return result


def _identify_refs(content: dict) -> List[str]:
    refs = _find_refs(content)
    external_refs = [k for k, v in refs.items() if not v.startswith("#")]
    remaining_internal_refs = {k: v for k, v in refs.items() if v.startswith("#")}

    # Order internal references by dependency
    internal_refs = []
    while remaining_internal_refs:
        # ref_to_add will only contain a reference whose referenced section does not contain any remaining_internal_refs
        ref_to_add = None
        for potential_ref, ref_path in remaining_internal_refs.items():
            # ref_json_path is the JSON path to the content referenced by potential_ref
            ref_json_path = ref_path.replace("#", "$").replace("/", ".")
            # potential_ref has a dependency on an entry of remaining_internal_refs if that entry descends from ref_json_path
            dependencies = [
                r
                for r in remaining_internal_refs
                if r != potential_ref and _is_descendant(r, ref_json_path)
            ]
            if not dependencies:
                ref_to_add = potential_ref
                break
        if ref_to_add is None:
            raise ValueError(
                f'Likely circular dependency in $refs; could not add any of the refs {{{", ".join(remaining_internal_refs)}}} to dependency list of [{" <- ".join(internal_refs)}]'
            )
        internal_refs.append(ref_to_add)
        del remaining_internal_refs[ref_to_add]

    return external_refs + internal_refs


def _find_refs(content: Union[dict, list], root: str = "$") -> Dict[str, str]:
    paths = {}
    if isinstance(content, dict):
        if "$ref" in content and isinstance(content["$ref"], str):
            paths[root] = content["$ref"]
        for k, v in content.items():
            if _should_recurse(v):
                paths = dict(paths, **_find_refs(v, root + "." + k))
    else:
        for i, item in enumerate(content):
            if _should_recurse(item):
                paths = dict(paths, **_find_refs(item, root + f"[{i}]"))
    return paths


def _replace_refs(
    content: dict,
    context_file_name: str,
    ref_parent_paths: List[str],
    allof_paths: List[str],
    cache: Optional[Dict[str, dict]] = None,
) -> None:
    for path in ref_parent_paths:
        parent = [m.value for m in bc_jsonpath_ng.parse(path).find(content)]
        if len(parent) != 1:
            raise RuntimeError(
                f'Unexpectedly found {len(parent)} matches for $ref parent JSON Path "{path}"'
            )
        parent = parent[0]
        ref_path = parent.pop("$ref")
        if not ref_path.startswith("#"):
            ref_content, _ = _load_dict_with_references_from_file_name(
                ref_path, context_file_name, cache
            )
        else:
            ref_json_path = bc_jsonpath_ng.parse(
                ref_path.replace("#", "$").replace("/", ".")
            )
            ref_content = [m.value for m in ref_json_path.find(content)]
            if len(ref_content) != 1:
                raise RuntimeError(
                    f'Unexpectedly found {len(ref_content)} matches for local $ref path "{ref_path}" in {context_file_name}'
                )
            ref_content = ref_content[0]
        for k, v in ref_content.items():
            parent[k] = v

        # See if there is an allOf to resolve and resolve it if so
        if path.split(".")[-1].startswith("allOf["):
            allof_parent_path = ".".join(path.split(".")[0:-1])
            if allof_parent_path + ".allOf" in allof_paths:
                allof_parent_content = [
                    m.value
                    for m in bc_jsonpath_ng.parse(allof_parent_path).find(content)
                ]
                if len(allof_parent_content) != 1:
                    raise RuntimeError(
                        f'Unexpectedly found {len(ref_content)} matches for allOf parent path "{ref_path}"'
                    )
                allof_parent_content = allof_parent_content[0]
                if all("$ref" not in s for s in allof_parent_content["allOf"]):
                    # This allOf is complete and can be resolved
                    schemas = allof_parent_content.pop("allOf")
                    for schema in schemas:
                        for k, v in schema.items():
                            allof_parent_content[k] = v


def _select_path(content: dict, path: str) -> dict:
    if not path.startswith("/"):
        raise ValueError(
            f'Relative path to dict component must start with /; found instead: "{path}"'
        )
    path = path[1:]
    if "/" not in path:
        if not path in content:
            raise KeyError(
                f'Could not find key "{path}" in dict; found keys: {", ".join(content)}'
            )
        return content[path]
    else:
        separator_location = path.index("/")
        component = path[0:separator_location]
        subpath = path[separator_location:]
        if component not in content:
            raise KeyError(
                f'Could not find key "{path}" in dict content: {str(content)}'
            )
        return _select_path(content[component], subpath)


def _identify_allofs(content: Union[dict, list], root: str = "$") -> List[str]:
    paths = []
    if isinstance(content, dict):
        if (
            "allOf" in content
            and isinstance(content["allOf"], list)
            and all(
                isinstance(s, dict) and "$ref" in s and isinstance(s["$ref"], str)
                for s in content["allOf"]
            )
        ):
            paths.append(root + ".allOf")
        for k, v in content.items():
            if k == "allOf":
                continue
            if _should_recurse(v):
                paths.extend(_identify_allofs(v, root + "." + k))
    else:
        for i, item in enumerate(content):
            if _should_recurse(item):
                paths.extend(_identify_allofs(item, root + f"[{i}]"))
    return paths

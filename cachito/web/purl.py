# SPDX-License-Identifier: GPL-3.0-or-later
import os
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pkg_resources

from cachito.errors import ContentManifestError

if TYPE_CHECKING:
    from cachito.web.content_manifest import Package

PARENT_PURL_PLACEHOLDER = "PARENT_PURL"


def to_purl(package, parent_package_rel_path=None):
    """
    Generate the PURL representation of the package.

    :param Package package: the Package object
    :param parent_package_rel_path: Relative path from repo root to package root
    :return: the PURL string of the Package object
    :rtype: str
    :raise ContentManifestError: if the there is no implementation for the package type
    """
    if package.type in ("go-package", "gomod"):
        return _to_purl_go(package, parent_package_rel_path)
    elif package.type in ("npm", "yarn"):
        return _to_purl_npm(package)
    elif package.type == "pip":
        return _to_purl_pip(package)
    elif package.type == "rubygems":
        return _to_purl_rubygems(package, parent_package_rel_path)
    elif package.type == "git-submodule":
        return _to_purl_git(package)
    else:
        raise ContentManifestError(f"The PURL spec is not defined for {package.type} packages")


def _to_purl_go(package: "Package", parent_package_rel_path: Optional[Path] = None) -> str:
    if package.version and package.version.startswith("."):
        if parent_package_rel_path is None:
            return f"{PARENT_PURL_PLACEHOLDER}"
        # Package is relative to the parent module
        else:
            return f"{PARENT_PURL_PLACEHOLDER}#{parent_package_rel_path}"

    # Use only the PURL "name" field to avoid ambiguity for Go modules/packages
    # see https://github.com/package-url/purl-spec/issues/63 for further reference
    purl_name = urllib.parse.quote(package.name, safe="")
    if package.version:
        return f"pkg:golang/{purl_name}@{package.version}"
    else:
        return f"pkg:golang/{purl_name}"


def _to_purl_npm(package):
    purl_name = urllib.parse.quote(package.name)
    match = re.match(r"(?P<protocol>[^:]+):(?P<has_authority>//)?(?P<suffix>.+)", package.version)
    if not match:
        return f"pkg:npm/{purl_name}@{package.version}"
    protocol = match.group("protocol")
    suffix = match.group("suffix")
    has_authority = match.group("has_authority")
    if protocol == "file":
        path = urllib.parse.urlparse(package.version).path
        quoted_path = urllib.parse.quote(path, safe="")
        return f"pkg:generic/{purl_name}?file={quoted_path}"
    elif not has_authority:
        # github:namespace/name#ref or gitlab:ns1/ns2/name#ref
        match_forge = re.match(r"(?P<namespace>.+)/(?P<name>[^#/]+)#(?P<version>.+)$", suffix)
        if not match_forge:
            raise ContentManifestError(f"Could not convert version {package.version} to purl")
        forge = match_forge.groupdict()
        return f"pkg:{protocol}/{forge['namespace']}/{forge['name']}@{forge['version']}"
    elif protocol in ("git", "git+http", "git+https", "git+ssh"):
        qualifier = urllib.parse.quote(package.version, safe="")
        return f"pkg:generic/{purl_name}?vcs_url={qualifier}"
    elif protocol in ("http", "https"):
        qualifier = urllib.parse.quote(package.version, safe="")
        return f"pkg:generic/{purl_name}?download_url={qualifier}"
    else:
        raise ContentManifestError(
            f"Unknown protocol in {package.type} package version: {package.version}"
        )


def _to_purl_pip(package):
    # As per the purl spec, PyPI names should be normalized by lowercasing and
    # converting '_' to '-'. The safe_name() function does the latter but not the
    # former. It is not necessary to escape characters in the name, safe_name()
    # also replaces everything except alphanumeric chars and '.' with '-'.
    name = pkg_resources.safe_name(package.name.lower())
    parsed_url = urllib.parse.urlparse(package.version)

    if not parsed_url.scheme:
        # Version is a PyPI version string
        return f"pkg:pypi/{name}@{package.version}"
    elif parsed_url.scheme.startswith("git+"):
        # Version is git+<git_url>
        return to_git_purl(package.name, parsed_url)
    else:
        # Version is a plain URL
        fragments = urllib.parse.parse_qs(parsed_url.fragment)
        checksum = fragments["cachito_hash"][0]
        quoted_url = urllib.parse.quote(package.version, safe="")
        return f"pkg:generic/{name}?download_url={quoted_url}&checksum={checksum}"


def _to_purl_rubygems(package, parent_package_rel_path):
    if package.version and package.version.startswith("./"):
        # Package is relative to the parent module
        path_dep_rel_path = os.path.normpath(package.version)
        if parent_package_rel_path is None:
            return f"{PARENT_PURL_PLACEHOLDER}#{path_dep_rel_path}"
        else:
            return f"{PARENT_PURL_PLACEHOLDER}#{parent_package_rel_path}/{path_dep_rel_path}"
    parsed_url = urllib.parse.urlparse(package.version)
    if parsed_url.scheme.startswith("git+"):
        return to_git_purl(package.name, parsed_url)
    else:
        return f"pkg:gem/{package.name}@{package.version}"


def to_git_purl(pkg_name, parsed_url):
    """
    Parse parsed_url into a PURL.

    :param pkg_name: Name of the git package
    :param parsed_url: Expected format is "git+<url>@<hash>
    :return Git PURL
    """
    scheme = parsed_url.scheme[len("git+") :]
    vcs_url = f"{scheme}://{parsed_url.netloc}{parsed_url.path}"
    repo_url, ref = vcs_url.rsplit("@", 1)
    return to_vcs_purl(pkg_name, repo_url, ref)


def _to_purl_git(package):
    # Version is a submodule repository url followed by `#` separator and
    # `submodule-commit-ref`, e.g.
    # https://github.com/org-name/submodule-name.git#522fb816eec295ad58bc488c74b2b46748d471b2
    repo_url, ref = package.version.rsplit("#", 1)
    return to_vcs_purl(package.name, repo_url, ref)


def to_vcs_purl(pkg_name, repo_url, ref):
    """
    Generate the vcs purl representation of the package.

    Use the most specific purl type possible, e.g. pkg:github if repo comes from
    github.com. Fall back to using pkg:generic with a ?vcs_url qualifier.

    :param str pkg_name: name of package
    :param str repo_url: url of git repository for package
    :param str ref: git ref of package
    :return: the PURL string of the Package object
    :rtype: str
    """
    repo_url = repo_url.rstrip("/")
    parsed_url = urllib.parse.urlparse(repo_url)

    pkg_type_for_hostname = {
        "github.com": "github",
        "bitbucket.org": "bitbucket",
    }
    pkg_type = pkg_type_for_hostname.get(parsed_url.hostname, "generic")

    if pkg_type == "generic":
        vcs_url = urllib.parse.quote(f"{repo_url}@{ref}", safe="")
        purl = f"pkg:generic/{pkg_name}?vcs_url={vcs_url}"
    else:
        # pkg:github and pkg:bitbucket use the same format
        namespace, repo = parsed_url.path.lstrip("/").rsplit("/", 1)
        if repo.endswith(".git"):
            repo = repo[: -len(".git")]
        purl = f"pkg:{pkg_type}/{namespace.lower()}/{repo.lower()}@{ref}"

    return purl


def to_top_level_purl(package, request, subpath=None):
    """
    Generate the purl representation of a top-level package (not a dependency).

    In Cachito, all top-level packages come from the git repository that the user
    requested. Generate a purl that properly conveys this information.

    The relation between Package and Request is many-to-many, therefore the caller
    must specify the request to use when generating the purl.

    :param Package package: a Package object
    :param Request request: the request that contains this package
    :param str subpath: relative path to package from root of repository
    :return: the PURL string of the Package object
    :rtype: str
    """
    if package.type in ("gomod", "go-package", "git-submodule"):
        purl = to_purl(package)
        # purls for git submodules point to a different repo, path is neither needed nor valid
        # golang package and module names should reflect the path already
        include_path = False
    elif package.type in ("npm", "pip", "yarn", "rubygems"):
        purl = to_vcs_purl(package.name, request.repo, request.ref)
        include_path = True
    else:
        raise ContentManifestError(f"{package.type!r} is not a valid top level package")

    if subpath and include_path:
        purl = f"{purl}#{subpath}"

    return purl


def replace_parent_purl_placeholder(dep_purl, parent_purl):
    """Replace PARENT_PURL_PLACEHOLDER in dependency with the parent purl."""
    return dep_purl.replace(PARENT_PURL_PLACEHOLDER, parent_purl)

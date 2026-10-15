# Third-party notices

The original integration, configuration, orchestration, tests, and reporting code
in this repository is licensed under the [MIT License](LICENSE).
Third-party software retains its own copyright and license.

The supplier entry point installs these pinned direct Python dependencies from
the package index; this source export does not bundle their source distributions,
wheels, or installed environments:

| Dependency | Version | License | Official license |
| --- | --- | --- | --- |
| EvalScope | 1.13.0, with `perf` extras | Apache-2.0 | [LICENSE](https://github.com/modelscope/evalscope/blob/v1.13.0/LICENSE) |
| Pillow | 12.3.0 | MIT-CMU | [LICENSE](https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE) |
| PyYAML | 6.0.3 | MIT | [LICENSE](https://github.com/yaml/pyyaml/blob/6.0.3/LICENSE) |

License identifiers and upstream notices were checked against these release files
and official package metadata on 2026-10-15. Pillow 12.3.0 identifies its license as
MIT-CMU; older references may describe it using a different historical label.

Transitive dependencies and optional tools are subject to the licenses included
in their respective packages. The supplier bootstrap does not install Node.js or
Promptfoo. The small `package.json` is retained because a shared preflight module
reads its version metadata; it is not a bundled installation of that tool.

Do not interpret this repository's MIT license as replacing the licenses of any
third-party dependencies. If distributing a preinstalled environment, container,
or bundled executable, include the relevant dependency notices and licenses for
that distribution as well.

#!/usr/bin/env python
"""Python package description."""

from pathlib import Path

from setuptools import setup

setup(
    name="pyporscheconnectapi-bartolije",
    version="1.0.0",
    author="Jeremie Bartoli",
    description=(
        "Python library and CLI for communicating with Porsche Connect API "
        "(fork of CJNE/pyporscheconnectapi by Johan Isacsson)."
    ),
    long_description=Path("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    include_package_data=True,
    url="https://github.com/bartolije/pyporscheconnectapi",
    project_urls={
        "Source": "https://github.com/bartolije/pyporscheconnectapi",
        "Issues": "https://github.com/bartolije/pyporscheconnectapi/issues",
        "Original project": "https://github.com/CJNE/pyporscheconnectapi",
    },
    license_expression="MIT",
    packages=["pyporscheconnectapi"],
    python_requires=">=3.12",
    install_requires=[
        "httpx<1",
        "beautifulsoup4",
    ],
    # Only porschecli needs these; a library user (Home Assistant) does not.
    extras_require={"cli": ["aiofiles", "rich"]},
    classifiers=[
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    ],
)

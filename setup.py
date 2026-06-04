#!/usr/bin/env python
"""Python package description."""

from pathlib import Path

from setuptools import setup

setup(
    name="pyporscheconnectapi-bartolije",
    version="0.3.1",
    author="Johan Isaksson",
    author_email="johan@generatorhallen.se",
    description="Python library and CLI for communicating with Porsche Connect API.",
    long_description=Path("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    include_package_data=True,
    url="https://github.com/cjne/pyporscheconnectapi",
    license="MIT",
    packages=["pyporscheconnectapi"],
    python_requires=">=3.12",
    install_requires=[
        "aiofiles",
        "httpx<1",
        "beautifulsoup4",
        "rich",
    ],
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)

"""setup.py for BrainrotFilter (Debian/Linux)."""

from pathlib import Path

from setuptools import find_packages, setup

here = Path(__file__).parent

# Read version from version.py
version_ns = {}
exec((here / "src" / "brainrotfilter" / "version.py").read_text(), version_ns)

setup(
    name="brainrotfilter",
    version=version_ns.get("__version__", "1.0.0"),
    description="Network-level YouTube brainrot video filter for Linux",
    long_description=(here / "README.md").read_text(encoding="utf-8")
    if (here / "README.md").exists()
    else "",
    long_description_content_type="text/markdown",
    url="https://github.com/IAmDestructoman/brainrotfilter-deb",
    author="IAmDestructoman",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Internet :: Proxy Servers",
        "Topic :: System :: Networking :: Firewalls",
    ],
    python_requires=">=3.9",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    install_requires=[
        "fastapi>=0.109.0",
        "uvicorn[standard]>=0.27.0",
        "jinja2>=3.1.3",
        "pydantic>=2.6.0",
        "aiohttp>=3.9.3",
        "httpx>=0.27.0",
    ],
)

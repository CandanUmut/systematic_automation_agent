
from setuptools import setup, find_packages
setup(
    name="static_hash_store",
    version="0.1.0",
    packages=find_packages(),
    install_requires=["numpy", "zstandard"],
    python_requires=">=3.9",
)

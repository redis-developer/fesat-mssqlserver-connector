# Copyright 2021 Redis Labs
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from os import path
from setuptools import find_packages

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

# README file from repo root directory
this_directory = path.abspath(path.dirname(__file__))
with open(path.join(this_directory, 'README.md'), encoding='utf-8') as f:
    LONG_DESCRIPTION = f.read()


setup(
    name="feast-mssqlserver-connector",
    author="Redis Labs",
    description="MS SQL Server Connector for Feast",
    long_description=LONG_DESCRIPTION,
    long_description_content_type="text/markdown",
    python_requires=">=3.7.0",
    url="https://github.com/redis-developer/feast-mssqlserver-connector",
    packages=find_packages(exclude=("tests",)),
    install_requires=[
        "feast>=0.12.0",
        "SQLAlchemy>=1.4.19",
        "pyodbc>=4.0.30",
    ],
    extras_require={
        "dev": [
            "pytest",
            "mypy",
            "assertpy"
        ]
    },
    # https://stackoverflow.com/questions/28509965/setuptools-development-requirements
    # Install dev requirements with: pip install -e .[dev]
    include_package_data=True,
    license="Apache",
    classifiers=[
        # Trove classifiers
        # Full list: https://pypi.python.org/pypi?%3Aaction=list_classifiers
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
    ],
)

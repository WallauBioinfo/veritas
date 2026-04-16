from setuptools import setup, find_packages

setup(
    name="veritas",
    description="""
    Tool to get benchmark datasets and compare VCF files.
    """,
    version="0.1.0",
    author="Filipe Dezordi",
    author_email="zimmer.filipe@gmail.com",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3.10",
        "Intended Audience :: Science/Research",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
    ],
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        "click==8.1.3",
        "pysam>=0.19.0",
        "pandas>=1.0.0",
        "requests>=2.28.0",
        "pyyaml>=6.0",
        "PyGithub>=2.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
        ]
    },
    entry_points={
        "console_scripts": [
            "veritas = veritas.commands.main:cli",
        ],
    },
)

from setuptools import setup, find_packages

setup(
    name="veritas",
    description="""
    Tool to get benchmark datasets and compare VCF files.
    """,
    version="0.0.1",
    authors="Filipe Dezordi",
    authors_emails="zimmer.filipe@gmail.com",
    classifiers=[
        "Development Status :: Prototype",
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
        "pytest>=7.0.0",
        "pytest-cov>=4.0.0",
    ],
    entry_points={
        "console_scripts": [
            "veritas = veritas.commands.main:cli",
        ],
    },
)

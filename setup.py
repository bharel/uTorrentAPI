import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="uTorrentAPI",
    version="0.1.0a",
    author="Bar Harel",
    python_requires='>=3.7',
    author_email="bzvi7919@gmail.com",
    description="uTorrent's unofficial asyncio API",
    long_description=long_description,
    long_description_content_type="text/markdown",
    install_requires=['aiohttp'],
    url="https://github.com/bharel/uTorrentAPI",
    packages=setuptools.find_packages(),
    classifiers=(
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    )
)

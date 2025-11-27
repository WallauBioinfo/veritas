import requests
import os
import base64
import yaml
import subprocess
import github
import glob
from veritas.logger import logger

BASIC_URL = f"https://api.github.com/repos"


def get_datasets_paths(url: str, headers: str) -> list[str]:
    """
    Get paths of all metadata.yaml files in the repository.

    Args:
        url: GitHub API URL
        headers: Request headers

    Returns:
        List of paths to metadata.yaml files
    """
    response = requests.get(f"{BASIC_URL}/{url}", headers=headers)
    response.raise_for_status()

    data = response.json()
    tree = data.get("tree", [])

    metadata = []

    for item in tree:
        if item["type"] == "blob":
            if item["path"].endswith("metadata.yaml"):
                metadata.append(item["path"])
    return metadata


def get_medata_information(url: str, paths: list[str], headers: str) -> list[dict]:
    """
    Retrieve metadata information from the repository.

    Args:
        url: GitHub API URL
        paths: List of paths to metadata.yaml files
        headers: Request headers

    Returns:
        List of dictionaries containing dataset metadata
    """
    datasets = []
    for path in paths:
        request_url = f"{BASIC_URL}/{url}/{path}"
        response = requests.get(request_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        content_encoded = data["content"]
        content = base64.b64decode(content_encoded).decode("utf-8")
        yaml_data = yaml.safe_load(content)
        yaml_data["dataset"] = path.strip("/metadata.yaml").strip("datasets/")
        datasets.append(yaml_data)

    return datasets


def print_formatted_table(data):
    """
    Print a formatted table of dataset information.

    Args:
        data: List of dictionaries containing dataset metadata
    """
    fields = [
        "dataset",
        "dataset_version",
        "pathogen",
        "sequencing_technology",
        "layout",
        "dataset_type",
        "strategy",
        "read_length",
    ]

    # Compute maximum width for each column
    col_widths = []
    for f in fields:
        max_len = max(len(str(item.get(f, ""))) for item in data)
        max_len = max(max_len, len(f))  # ensure header fits
        col_widths.append(max_len)

    header = (
        "| "
        + " | ".join(
            f"\033[1m{f:{col_widths[i]}}\033[0;0m" for i, f in enumerate(fields)
        )
        + " |"
    )
    separator = (
        "|-" + "-|-".join("-" * col_widths[i] for i in range(len(fields))) + "-|"
    )
    start_separator = (
        "┌-" + "-┬-".join("-" * col_widths[i] for i in range(len(fields))) + "-┐"
    )
    rows = []
    for item in data:
        row = (
            "| "
            + " | ".join(
                f"{str(item.get(f, '')):{col_widths[i]}}" for i, f in enumerate(fields)
            )
            + " |"
        )
        rows.append(row)
    end_separator = (
        "└-" + "-┴-".join("-" * col_widths[i] for i in range(len(fields))) + "-┘"
    )

    print(start_separator)
    print(header)
    print(separator)
    count_separator = 0
    for row in rows:
        print(row)
        if count_separator < len(rows) - 1:
            print(separator)
        count_separator += 1
    print(end_separator)


def download_dataset(repo_path, dataset_name, output_dir=None, token=None):
    """
    Download entire folder from GitHub with minimal API calls.

    Args:
        repo_path: Repository path (owner/repo)
        dataset_name: Name of the dataset to download
        output_dir: Local directory to save files
        token: GitHub token (optional, but recommended)
    """
    g = github.Github(auth=github.Auth.Token(token)) if token else github.Github()
    repo = g.get_repo(repo_path)

    dataset_name_formatted = (
        dataset_name.split("/")[-2] + "/" + dataset_name.split("/")[-1]
    )
    if not output_dir:
        output_dir = dataset_name_formatted
    else:
        output_dir = f"{output_dir}/{dataset_name_formatted}"
    os.makedirs(output_dir, exist_ok=True)

    contents = repo.get_contents(f"datasets/{dataset_name}")

    while contents:
        file_content = contents.pop(0)
        if file_content.type == "dir":
            subdir = file_content.path.split("/")[-1]
            output_dir = f"{output_dir}/{subdir}"
            os.makedirs(output_dir, exist_ok=True)
            contents.extend(repo.get_contents(file_content.path))
        else:
            local_path = os.path.join(output_dir, file_content.name)
            file_data = repo.get_contents(file_content.path)

            if hasattr(file_data, "content"):
                content = base64.b64decode(file_data.content)
                if file_content.name == "metadata.yaml":
                    yaml_data = yaml.safe_load(content)
                    if yaml_data["dataset_type"] == "real":
                        print("Real dataset detected, downloading from SRA.")
                        sra_id = yaml_data.get("sra")
                        sample = yaml_data.get("sample")
                        print(f"Downloading fastq for {sra_id}")
                        cmd = [
                            "parallel-fastq-dump",
                            "--sra-id",
                            sra_id,
                            "--threads",
                            "4",
                            "--outdir",
                            output_dir,
                            "--split-files",
                            "--gzip",
                        ]
                        subprocess.run(
                            cmd,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        print(f"Keeping paired reads on fastqs files for {sra_id}")
                        cmd = [
                            "seqkit",
                            "pair",
                            "-1",
                            f"{output_dir}/{sra_id}_1.fastq.gz",
                            "-2",
                            f"{output_dir}/{sra_id}_2.fastq.gz",
                        ]
                        subprocess.run(
                            cmd,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        cmd = [
                            "mv",
                            f"{output_dir}/{sra_id}_1.paired.fastq.gz",
                            f"{output_dir}/{sample}_R1.fastq.gz",
                        ]
                        subprocess.run(
                            cmd,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        cmd = [
                            "mv",
                            f"{output_dir}/{sra_id}_2.paired.fastq.gz",
                            f"{output_dir}/{sample}_R2.fastq.gz",
                        ]
                        subprocess.run(
                            cmd,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        cmd = [
                            "rm",
                            f"{output_dir}/{sra_id}_1.fastq.gz",
                            f"{output_dir}/{sra_id}_2.fastq.gz",
                        ]
                        subprocess.run(
                            cmd,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    elif yaml_data["dataset_type"] == "artificial":
                        print("Artificial dataset detected, downloading from Zenodo.")
                        zenodo_id = yaml_data.get("zenodo_id")
                        print(f"Fetching file list from Zenodo record {zenodo_id}...")

                        # Get file list from Zenodo API
                        api_url = f"https://zenodo.org/api/records/{zenodo_id}"
                        response = requests.get(api_url)
                        response.raise_for_status()
                        zenodo_data = response.json()

                        # Filter for .fastq.gz files
                        fastq_files = [
                            f
                            for f in zenodo_data.get("files", [])
                            if f["key"].endswith(".fastq.gz")
                        ]

                        if not fastq_files:
                            print(
                                f"Warning: No .fastq.gz files found in Zenodo record {zenodo_id}"
                            )
                        else:
                            print(
                                f"Found {len(fastq_files)} fastq.gz file(s) to download"
                            )

                            for file_info in fastq_files:
                                file_name = file_info["key"]
                                file_url = file_info["links"]["self"]
                                output_path = os.path.join(output_dir, file_name)

                                print(f"Downloading {file_name}...")
                                cmd = [
                                    "wget",
                                    file_url,
                                    "-O",
                                    output_path,
                                ]
                                subprocess.run(
                                    cmd,
                                    check=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                                print(f"   Downloaded {file_name}")
            else:
                content = file_data.decoded_content

            with open(local_path, "wb") as f:
                f.write(content)


def find_dataset_file(dataset_dir, pattern, file_description):
    """
    Find a file in the dataset directory matching the pattern.

    Args:
        dataset_dir: Directory to search in
        pattern: Glob pattern to match
        file_description: Description of the file for logging

    Returns:
        Path to the found file, or None if not found
    """
    if not dataset_dir:
        return None
    matches = glob.glob(os.path.join(dataset_dir, pattern))
    if matches:
        logger.info(f"Found {file_description}: {matches[0]}")
        return matches[0]
    return None


def find_dataset_dir(dataset_dir, dir_name, dir_description):
    """
    Find a directory in the dataset directory.

    Args:
        dataset_dir: Directory to search in
        dir_name: Name of the directory to find
        dir_description: Description of the directory for logging

    Returns:
        Path to the found directory, or None if not found
    """
    if not dataset_dir:
        return None
    dir_path = os.path.join(dataset_dir, dir_name)
    if os.path.isdir(dir_path):
        logger.info(f"Found {dir_description}: {dir_path}")
        return dir_path
    return None

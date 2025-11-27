# Veritas Tests

This directory contains the test suite for Veritas.

## Test Structure

```
tests/
├── conftest.py           # Pytest fixtures and configuration
├── test_validate.py      # Tests for validation functions
├── test_datasets.py      # Tests for dataset management
├── test_commands.py      # Tests for CLI commands
├── test_metrics.py       # Tests for metrics calculation
├── fixtures/             # Test data files
└── README.md            # This file
```

## Running Tests

### Run all tests
```bash
pytest tests/ -v
```

### Run specific test file
```bash
pytest tests/test_validate.py -v
```

### Run specific test class
```bash
pytest tests/test_validate.py::TestComputeMetrics -v
```

### Run specific test
```bash
pytest tests/test_validate.py::TestComputeMetrics::test_perfect_match -v
```

## Test Categories

### Unit Tests
- `test_validate.py`: Tests for core validation functions
  - `compute_metrics()`: Precision, recall, F1-score calculations
  - `get_bed_intervals()`: BED file parsing
  - `process_gsalign_vcf()`: VCF processing
  - `in_bed_region()`: Position checking

- `test_datasets.py`: Tests for dataset functions
  - `print_formatted_table()`: Metadata table formatting

- `test_metrics.py`: Tests for metrics functions
  - `calc_metrics()`: VCF metrics calculation
  - `save_metrics_tsv()`: TSV export

### Integration Tests
- `test_commands.py`: Tests for CLI commands
  - `validate`: Query validation command
  - `convert`: FASTA to VCF conversion
  - `rtg-format`: Reference formatting
  - `list-datasets`: Dataset listing
  - `get-dataset`: Dataset download

## Fixtures

The `conftest.py` file provides the following fixtures:

- `temp_dir`: Temporary directory for test files
- `sample_reference_fasta`: Sample reference genome
- `sample_query_fasta`: Sample query FASTA with variants
- `sample_truth_vcf`: Sample truth VCF file
- `sample_query_vcf`: Sample query VCF with TP/FP variants
- `sample_primer_bed`: Sample primer BED file
- `sample_mask_bed`: Sample mask BED file
- `sample_bed_intervals`: Sample BED intervals as tuples
- `sample_yaml_metadata`: Sample metadata dictionary

## Writing New Tests

### Basic Test Structure

```python
import pytest
from veritas.module import function_to_test

class TestFunctionName:
    """Tests for function_to_test."""
    
    def test_basic_case(self):
        """Test basic functionality."""
        result = function_to_test(input_data)
        assert result == expected_output
    
    def test_edge_case(self):
        """Test edge case."""
        result = function_to_test(edge_input)
        assert result == expected_edge_output
```

### Using Fixtures

```python
def test_with_fixture(self, temp_dir, sample_reference_fasta):
    """Test using fixtures."""
    # Use the fixtures in your test
    output_file = os.path.join(temp_dir, "output.txt")
    process_file(sample_reference_fasta, output_file)
    assert os.path.exists(output_file)
```

### Testing CLI Commands

```python
from click.testing import CliRunner
from veritas.commands.main import cli

def test_command():
    """Test CLI command."""
    runner = CliRunner()
    result = runner.invoke(cli, ['command', '--option', 'value'])
    assert result.exit_code == 0
    assert 'expected output' in result.output
```

## Continuous Integration

These tests are run automatically on:
- Every pull request
- Every push to main branch
- On scheduled nightly builds

## Troubleshooting

### Tests fail due to missing dependencies

Make sure you have installed Veritas with all dependencies:
```bash
pip install -e .
```

### Tests fail with "command not found"

Some tests require external tools (RTG, GSAlign, etc.). These tests should be skipped if tools are not available:
```bash
pytest tests/ -v -k "not rtg and not gsalign"
```

### Temporary files not cleaned up

If tests crash, temporary files might remain. Clean them manually:
```bash
rm -rf /tmp/pytest-*
```

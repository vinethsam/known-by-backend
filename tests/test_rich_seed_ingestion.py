"""Rich inputs keep their exact audit trail while only useful context is public."""

import csv
import io

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.config import Settings
from app.export.batch import parse_batch
from app.schemas import PersonSeed
from app.seed_context import (
    classify_seed_label,
    seed_context_values,
    seed_core_values,
    seed_input_attributes,
)


def parse_record(record: dict[str, str], format: str = "csv"):
    if format == "xlsx":
        workbook = Workbook()
        workbook.active.append(list(record))
        workbook.active.append(list(record.values()))
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        content = output.getvalue()
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(record)
        writer.writerow(record.values())
        content = output.getvalue().encode("utf-8-sig")
    return parse_batch(content, f"input.{format}", None, Settings(APP_ENV="test"))


@pytest.mark.parametrize("format", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "extra, expected",
    [
        ({}, {}),
        ({"Company Name": "Aster Labs"}, {"organisation": ["Aster Labs"]}),
        ({"Educational Institution": "Lake University"}, {"university_name": ["Lake University"]}),
        ({"Current Job Title": "Research Director"}, {"job_title": ["Research Director"]}),
        (
            {"University": "Lake University", "Field of Study": "Physics"},
            {"university_name": ["Lake University"], "subject": ["Physics"]},
        ),
        (
            {
                "job_company_name": "Aster Labs",
                "job_title": "Research Director",
                "education_school_name": "Lake University",
                "Qualification": "Master of Science",
                "education_major": "Physics",
                "Professional Field": "Renewable Energy",
                "location": "Colombo",
            },
            {
                "organisation": ["Aster Labs"],
                "job_title": ["Research Director"],
                "university_name": ["Lake University"],
                "degree_type": ["Master of Science"],
                "subject": ["Physics"],
                "industry": ["Renewable Energy"],
                "location": ["Colombo"],
            },
        ),
    ],
)
def test_representative_records_preserve_context_in_both_formats(format, extra, expected):
    record = {"Full Name": "Maya Silva", **extra}
    batch = parse_record(record, format)

    assert batch.rows == [record]
    assert batch.columns == list(record)
    assert batch.seeds[0].input_metadata == record
    assert seed_context_values(batch.seeds[0]) == expected
    attributes = seed_input_attributes(batch.seeds[0])
    assert {attribute["column"]: attribute["value"] for attribute in attributes} == record
    assert all(attribute["input_confidence"] == 100 for attribute in attributes)
    assert all(attribute["provenance_status"] == "supplied" for attribute in attributes)


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_original_columns_and_whitespace_survive_normalized_research_context(format):
    record = {
        " Full Name ": "  Maya Silva  ",
        " UNIVERSITY_NAME ": "  Lake University  ",
        "DegreeType": "  MSc  ",
        " Research Notes ": "  exact text\n  second line  ",
    }
    batch = parse_record(record, format)
    seed = batch.seeds[0]

    assert seed.full_name == "Maya Silva"
    assert seed.university_name == "Lake University"
    assert seed.degree_type == "MSc"
    assert batch.columns == list(record)
    assert batch.rows[0] == seed.input_metadata == record
    assert {item["column"]: item["value"] for item in seed_input_attributes(seed)} == record


def test_private_administrative_and_unknown_values_are_preserved_but_not_researched():
    record = {
        "Name": "Maya Silva",
        "University": "Lake University",
        "LIST_NAME": "Private Donor Group",
        "record_id": "record-123",
        "Batch Label": "Pilot 2026",
        "Source Dataset": "Internal Export",
        "Email Address": "private@example.test",
        "Mobile Phone": "+94 71 123 4567",
        "Home Address": "123 Quiet Road",
        "Arbitrary Custom Attribute": "Private arbitrary value",
    }
    seed = parse_record(record).seeds[0]
    attributes = {item["column"]: item for item in seed_input_attributes(seed)}

    assert len(attributes) == len(record)
    assert attributes["LIST_NAME"]["category"] == "administrative"
    assert attributes["Email Address"]["category"] == "sensitive"
    assert attributes["Arbitrary Custom Attribute"]["category"] == "unknown"
    assert attributes["Arbitrary Custom Attribute"]["field"] is None
    assert {label: item["value"] for label, item in attributes.items()} == record
    assert all(item["input_confidence"] == 100 for item in attributes.values())
    assert seed_context_values(seed) == {"university_name": ["Lake University"]}


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_country_context_and_historical_employment_are_not_promoted(format):
    record = {
        "Name": "Maya Silva",
        "Country": "Canada",
        "Country of Origin": "Sri Lanka",
        "Previous Employer": "Former Labs",
        "Former Job Title": "Research Associate",
    }
    seed = parse_record(record, format).seeds[0]

    assert seed.country == "Canada"
    assert seed.country_of_origin == "Sri Lanka"
    assert seed.historical_employer == "Former Labs"
    assert seed.historical_job_title == "Research Associate"
    assert seed.organisation is None
    assert seed.job_title is None
    assert seed_core_values(seed) == {"full_name": "Maya Silva", "country_of_origin": "Sri Lanka"}
    assert seed_context_values(seed)["historical_employer"] == ["Former Labs"]
    assert seed_context_values(seed)["historical_job_title"] == ["Research Associate"]


def test_direct_api_seed_captures_raw_values_and_colliding_known_attribute_labels():
    seed = PersonSeed(
        full_name="  Maya Silva  ",
        university_name="  Lake University  ",
        known_attributes={
            "university_name": "  Hill University  ",
            "LIST_NAME": "  Restricted Cohort  ",
            "personal_email": "  private@example.test  ",
            "unrecognized detail": "  keep me  ",
        },
    )
    supplied = {item["column"]: item for item in seed_input_attributes(seed)}

    assert supplied["full_name"]["value"] == "  Maya Silva  "
    assert supplied["university_name"]["value"] == "  Lake University  "
    assert supplied["known:university_name"]["value"] == "  Hill University  "
    assert supplied["personal_email"]["category"] == "sensitive"
    assert supplied["unrecognized detail"]["value"] == "  keep me  "
    assert all(item["input_confidence"] == 100 for item in supplied.values())
    assert set(seed_context_values(seed)) == {"university_name"}


@pytest.mark.parametrize("known_attributes", ["invalid", ["invalid"], 123])
def test_invalid_known_attributes_use_validation_errors(known_attributes):
    with pytest.raises(ValidationError):
        PersonSeed(full_name="Maya Silva", known_attributes=known_attributes)


def test_partial_input_metadata_does_not_discard_explicit_typed_api_values():
    seed = PersonSeed(
        full_name="  Maya Silva  ",
        university_name="  Lake University  ",
        known_attributes={"Field of Study": "  Physics  "},
        input_metadata={"Custom Annotation": "  raw private annotation  "},
    )
    supplied = {item["column"]: item for item in seed_input_attributes(seed)}

    assert supplied["Custom Annotation"]["value"] == "  raw private annotation  "
    assert supplied["full_name"]["value"] == "  Maya Silva  "
    assert supplied["university_name"]["value"] == "  Lake University  "
    assert supplied["Field of Study"]["value"] == "  Physics  "
    assert all(item["input_confidence"] == 100 for item in supplied.values())


def test_equally_strong_aliases_remain_distinct_without_guessing_single_core_value():
    seed = parse_record(
        {"Name": "Maya Silva", "University Name": "Lake University", "College Name": "Hill College"}
    ).seeds[0]

    assert seed.university_name is None
    assert seed_context_values(seed)["university_name"] == ["Lake University", "Hill College"]
    assert seed.input_metadata["University Name"] == "Lake University"
    assert seed.input_metadata["College Name"] == "Hill College"


@pytest.mark.parametrize("value", ["private@example.test", "+94 71 123 4567", "123 Quiet Road"])
def test_contact_like_values_are_not_public_context_even_under_a_recognized_label(value):
    seed = PersonSeed(full_name="Maya Silva", location=value)

    assert seed.input_metadata["location"] == value
    assert seed_context_values(seed) == {}


def test_context_is_bounded_and_unknown_labels_are_not_arbitrarily_mapped():
    context = seed_context_values(
        {
            "university_name": [f"University {index}" for index in range(100)],
            "input_metadata": {"Unknown Private Detail": "Secret Context"},
            "input_field_map": {"Unknown Private Detail": "organisation"},
        }
    )

    assert len(context["university_name"]) == 20
    assert "organisation" not in context
    assert sum(len(value) for values in context.values() for value in values) <= 2200
    assert classify_seed_label("Unknown Private Detail") is None


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_long_recognized_optional_values_remain_metadata_without_blocking_record(format):
    record = {"Name": "Maya Silva", "University": "Long University Name " * 20}
    seed = parse_record(record, format).seeds[0]

    assert seed.university_name is None
    assert seed.input_metadata == record
    assert seed_context_values(seed) == {}
    assert seed_input_attributes(seed)[1]["input_confidence"] == 100


def test_conflicting_explicit_api_values_and_input_metadata_are_both_preserved():
    seed = PersonSeed(
        full_name="Maya Silva",
        university_name="Lake University",
        input_metadata={"University Name": "Hill College"},
        known_attributes={"university_name": "Third University"},
    )
    attributes = seed_input_attributes(seed)
    assert {item["value"] for item in attributes} == {
        "Maya Silva",
        "Lake University",
        "Hill College",
        "Third University",
    }
    assert all(item["input_confidence"] == 100 for item in attributes)
    assert PersonSeed.model_validate_json(seed.model_dump_json()).input_metadata == seed.input_metadata


@pytest.mark.parametrize("url", ["https://example.test", "  HTTPS://Example.TEST/profile  "])
def test_preferred_url_raw_text_survives_normalization_and_model_round_trips(url):
    seed = PersonSeed(full_name="Maya Silva", preferred_urls=[url])
    original_metadata = dict(seed.input_metadata)

    assert original_metadata["preferred_url_1"] == url
    for _ in range(3):
        seed = PersonSeed.model_validate_json(seed.model_dump_json())
        assert seed.input_metadata == original_metadata
    attributes = {item["column"]: item for item in seed_input_attributes(seed)}
    assert attributes["preferred_url_1"]["input_confidence"] == 100


@pytest.mark.parametrize("original", ["private annotation", "https://different.test/profile"])
def test_preferred_url_preserves_conflicting_metadata_with_idempotent_collision_labels(original):
    seed = PersonSeed(
        full_name="Maya Silva",
        preferred_urls=["https://example.test"],
        input_metadata={"preferred_url_1": original, "known:preferred_url_1": "other supplied value"},
    )
    original_metadata = dict(seed.input_metadata)

    assert original_metadata["preferred_url_1"] == original
    assert original_metadata["known:preferred_url_1"] == "other supplied value"
    assert original_metadata["known:known:preferred_url_1"] == "https://example.test"
    for _ in range(3):
        seed = PersonSeed.model_validate_json(seed.model_dump_json())
        assert seed.input_metadata == original_metadata

# -*- coding: utf-8 -*-

import os
import json
import logging
import tempfile
import typing
from dateutil.parser import isoparse  # type: ignore
import string


import pytest
import requests
import pandas as pd
import numpy as np
from unittest import mock
from click.testing import CliRunner
from sklearn.base import BaseEstimator
from mock import patch, call

from gordo.client import Client, utils as client_utils
from gordo.machine import Machine
from gordo.client.io import _handle_response, HttpUnprocessableEntity, BadRequest
from gordo.client.forwarders import ForwardPredictionsIntoInflux
from gordo.client.utils import PredictionResult
from gordo.machine.dataset.data_provider import providers
from gordo.server import utils as server_utils
from gordo.machine.model import utils as model_utils
from gordo import cli, serializer
from gordo.cli import custom_types


def test_client_get_metadata(gordo_project, ml_server):
    """
    Test client's ability to get metadata from some target
    """
    client = Client(project=gordo_project)

    metadata = client.get_metadata()
    assert isinstance(metadata, dict)

    # Can't get metadata for non-existent target
    with pytest.raises(ValueError):
        client = Client(project=gordo_project, target="no-such-target")
        client.get_metadata()


def test_client_predict_specific_targets(gordo_project, gordo_single_target, ml_server):
    """
    Client.predict should filter any endpoints given to it.
    """
    client = Client(project=gordo_project)
    with mock.patch.object(
        Client,
        "predict_single_machine",
        return_value=PredictionResult("test-name", [], []),
    ) as patched:

        start = (isoparse("2016-01-01T00:00:00+00:00"),)
        end = isoparse("2016-01-01T12:00:00+00:00")

        # Should not actually call any predictions because this machine name doesn't exist
        client.predict(start=start, end=end, machine_names=["non-existant-machine"])
        patched.assert_not_called()

        # Should be called once, for this machine.
        client.predict(start=start, end=end, machine_names=[gordo_single_target])
        patched.assert_called_once()


def test_client_download_model(gordo_project, gordo_single_target, ml_server):
    """
    Test client's ability to download the model
    """
    client = Client(project=gordo_project, target=gordo_single_target)

    models = client.download_model()
    assert isinstance(models, dict)
    assert isinstance(models[gordo_single_target], BaseEstimator)

    # Can't download model for non-existent target
    with pytest.raises(ValueError):
        client = Client(project=gordo_project, target="non-existent-target")
        client.download_model()


@pytest.mark.parametrize("batch_size", (10, 100))
@pytest.mark.parametrize("use_parquet", (True, False))
def test_client_predictions_diff_batch_sizes(
    gordo_project,
    gordo_single_target,
    influxdb,
    influxdb_uri,
    influxdb_measurement,
    ml_server,
    batch_size: int,
    use_parquet: bool,
):
    """
    Run the prediction client with different batch-sizes and whether to use
    a data provider or not.
    """
    # Time range used in this test
    start, end = (
        isoparse("2016-01-01T00:00:00+00:00"),
        isoparse("2016-01-01T12:00:00+00:00"),
    )

    # Client only used within the this test
    test_client = client_utils.influx_client_from_uri(influxdb_uri)

    # Created measurements by prediction client with dest influx
    query = f"""
    SELECT *
    FROM "model-output"
    WHERE("machine" =~ /^{gordo_single_target}$/)
    """

    # Before predicting, influx destination db should be empty for 'predictions' measurement
    vals = test_client.query(query)
    assert len(vals) == 0

    data_provider = providers.InfluxDataProvider(
        measurement=influxdb_measurement,
        value_name="Value",
        client=client_utils.influx_client_from_uri(
            uri=influxdb_uri, dataframe_client=True
        ),
    )

    prediction_client = Client(
        project=gordo_project,
        data_provider=data_provider,
        prediction_forwarder=ForwardPredictionsIntoInflux(  # type: ignore
            destination_influx_uri=influxdb_uri
        ),
        batch_size=batch_size,
        use_parquet=use_parquet,
        parallelism=10,
    )

    # Should have discovered machine-1 and machine-2
    # defined in the example data of controller's response for /models/<project-name>
    assert len(prediction_client.machines) == 1

    # Get predictions
    predictions = prediction_client.predict(start=start, end=end)
    assert isinstance(predictions, list)
    assert len(predictions) == 1

    name, predictions, error_messages = predictions[0]  # First dict of predictions
    assert isinstance(name, str)
    assert isinstance(predictions, pd.DataFrame)
    assert isinstance(error_messages, list)

    assert isinstance(predictions.index, pd.core.indexes.datetimes.DatetimeIndex)

    # This should have resulted in writting predictions to influx
    # Before predicting, influx destination db should be empty
    vals = test_client.query(query)
    assert (
        len(vals) > 0
    ), f"Expected new values in 'predictions' measurement, but found {vals}"


@pytest.mark.parametrize(
    "args",
    [
        ["client", "--help"],
        ["client", "predict", "--help"],
        ["client", "metadata", "--help"],
        ["client", "download-model", "--help"],
    ],
)
def test_client_cli_basic(args):
    """
    Test that client specific subcommands exist
    """
    runner = CliRunner()
    out = runner.invoke(cli.gordo, args=args)
    assert (
        out.exit_code == 0
    ), f"Expected output code 0 got '{out.exit_code}', {out.output}"


def test_client_cli_metadata(gordo_project, gordo_single_target, ml_server, tmp_dir):
    """
    Test proper execution of client predict sub-command
    """
    runner = CliRunner()

    # Simple metadata fetching
    out = runner.invoke(
        cli.gordo,
        args=[
            "client",
            "--project",
            gordo_project,
            "--target",
            gordo_single_target,
            "metadata",
        ],
    )
    assert out.exit_code == 0
    assert gordo_single_target in out.output

    # Save metadata to file
    output_file = os.path.join(tmp_dir, "metadata.json")
    out = runner.invoke(
        cli.gordo,
        args=[
            "client",
            "--project",
            gordo_project,
            "--target",
            gordo_single_target,
            "metadata",
            "--output-file",
            output_file,
        ],
    )
    assert out.exit_code == 0, f"{out.exc_info}"
    assert os.path.exists(output_file)
    with open(output_file) as f:
        metadata = json.load(f)
        assert gordo_single_target in metadata


def test_client_cli_download_model(
    gordo_project, gordo_single_target, ml_server, tmp_dir
):
    """
    Test proper execution of client predict sub-command
    """
    runner = CliRunner()

    # Empty output directory before downloading
    assert len(os.listdir(tmp_dir)) == 0

    out = runner.invoke(
        cli.gordo,
        args=[
            "client",
            "--project",
            gordo_project,
            "--target",
            gordo_single_target,
            "download-model",
            tmp_dir,
        ],
    )
    assert (
        out.exit_code == 0
    ), f"Expected output code 0 got '{out.exit_code}', {out.output}"

    # Output directory should not be empty any longer
    assert len(os.listdir(tmp_dir)) > 0

    model_output_dir = os.path.join(tmp_dir, gordo_single_target)
    assert os.path.isdir(model_output_dir)

    model = serializer.load(model_output_dir)
    assert isinstance(model, BaseEstimator)


@pytest.mark.parametrize("use_forwarder", [True, False])
@pytest.mark.parametrize("output_dir", [True, False])
@pytest.mark.parametrize("use_parquet", (True, False))
@pytest.mark.parametrize("session_config", ({}, {"headers": {}}))
def test_client_cli_predict(
    influxdb,
    influxdb_uri,
    gordo_project,
    gordo_single_target,
    ml_server,
    tmp_dir,
    use_forwarder,
    trained_model_directory,
    output_dir,
    use_parquet,
    session_config,
):
    """
    Test ability for client to get predictions via CLI
    """
    runner = CliRunner()

    args = ["client", "--metadata", "key,value", "--project", gordo_project]
    if session_config:
        args.extend(["--session-config", json.dumps(session_config)])

    args.extend(
        [
            "predict",
            "--parquet" if use_parquet else "--no-parquet",
            "2016-01-01T00:00:00Z",
            "2016-01-01T01:00:00Z",
        ]
    )

    influx_client = client_utils.influx_client_from_uri(
        uri=influxdb_uri, dataframe_client=True
    )
    query = """
        SELECT *
        FROM "resampled"
        """

    # Do we have forwarder args?
    if use_forwarder:
        args.extend(["--influx-uri", influxdb_uri, "--forward-resampled-sensors"])
        vals = influx_client.query(query)
        # There is no data there before we start doing things
        assert len(vals) == 0

    # Should it write out the predictions to dataframes in an output directory?
    if output_dir:
        args.extend(["--output-dir", tmp_dir])

    # Do we have a data provider, POST else GET requests
    args.extend(
        ["--data-provider", json.dumps(providers.RandomDataProvider().to_dict())]
    )

    # Run without any error
    with patch(
        "gordo.machine.dataset.sensor_tag._asset_from_tag_name",
        side_effect=lambda *args, **kwargs: "default",
    ):
        out = runner.invoke(cli.gordo, args=args)
    assert out.exit_code == 0, f"{out.output}"

    # If we activated forwarder and we had any actual data then there should
    # be resampled values in the influx
    if use_forwarder:
        vals = influx_client.query(query)
        assert len(vals) == 1
        assert len(vals["resampled"]) == 48
        influx_client.drop_measurement("resampled")

    # Did it save dataframes to output dir if specified?
    if output_dir:
        assert os.path.exists(os.path.join(tmp_dir, f"{gordo_single_target}.csv.gz"))


@pytest.mark.parametrize(
    "should_fail,start_date,end_date",
    [
        (True, "1888-01-01T00:00:00Z", "1888-02-01T01:00:00Z"),  # Fail on bad dates
        (False, "2016-01-01T00:00:00Z", "2016-01-01T01:00:00Z"),  # pass on good dates
    ],
)
def test_client_cli_predict_non_zero_exit(
    should_fail,
    start_date,
    end_date,
    caplog,
    gordo_project,
    influxdb,
    influxdb_uri,
    influxdb_measurement,
    ml_server,
):
    """
    Test ability for client to get predictions via CLI
    """
    runner = CliRunner()

    # Should fail requesting dates which clearly don't exist.
    args = [
        "client",
        "--metadata",
        "key,value",
        "--project",
        gordo_project,
        "predict",
        start_date,
        end_date,
    ]

    data_provider = providers.InfluxDataProvider(
        measurement=influxdb_measurement, value_name="Value", uri=influxdb_uri
    )

    args.extend(["--data-provider", json.dumps(data_provider.to_dict())])

    # Run without any error
    with caplog.at_level(logging.CRITICAL):
        with patch(
            "gordo.machine.dataset.sensor_tag._asset_from_tag_name",
            side_effect=lambda *args, **kwargs: "default",
        ):
            out = runner.invoke(cli.gordo, args=args)

    if should_fail:
        assert out.exit_code != 0, f"{out.output or out.exception}"
    else:
        assert out.exit_code == 0, f"{out.output or out.exception}"


@pytest.mark.parametrize(
    "config",
    (
        '{"type": "RandomDataProvider", "max_size": 200}',
        '{"type": "InfluxDataProvider", "measurement": "value"}',
    ),
)
def test_data_provider_click_param(config, sensors_str):
    """
    Test click custom param to load a provider from a string config representation
    """
    expected_provider_type = json.loads(config)["type"]
    provider = custom_types.DataProviderParam()(config)
    assert isinstance(provider, getattr(providers, expected_provider_type))

    # Should also be able to take a file path with the json
    with tempfile.NamedTemporaryFile(mode="w") as config_file:
        json.dump(json.loads(config), config_file)
        config_file.flush()

        provider = custom_types.DataProviderParam()(config_file.name)
        assert isinstance(provider, getattr(providers, expected_provider_type))


@pytest.mark.parametrize("use_test_project_tags", [True, False])
def test_ml_server_dataframe_to_dict_and_back(sensors_str, use_test_project_tags):
    """
    Tests the flow of the server creating a dataframe from the model's data, putting into
    a dict of string to df. lists of values, and the client being able to reconstruct it back
    to the original dataframe (less the second level names)
    """
    # Run test with test project tag names
    if use_test_project_tags:
        tags = sensors_str
    # Run project with random names
    else:
        tags = [string.ascii_uppercase[i] for i in range(len(sensors_str))]

    # Some synthetic data
    original_input = np.random.random((10, len(tags)))
    model_output = np.random.random((10, len(tags)))

    # Convert this data into a dataframe with multi index columns
    df = model_utils.make_base_dataframe(tags, original_input, model_output)

    # Server then converts this into a dict which maps top level names to lists
    serialized = server_utils.dataframe_to_dict(df)

    # Client reproduces this dataframe
    df_clone = server_utils.dataframe_from_dict(serialized)

    # each subset of column under the top level names should be equal
    top_lvl_names = df.columns.get_level_values(0)
    for top_lvl_name in filter(lambda n: n not in ("start", "end"), top_lvl_names):
        assert np.allclose(df[top_lvl_name].values, df_clone[top_lvl_name].values)


def _machine(name: str) -> Machine:
    """
    Helper to build a basic Machine, only defining its name
    """
    from gordo.machine.dataset.sensor_tag import SensorTag

    return Machine.from_config(
        config={
            "name": name,
            "dataset": {
                "tag_list": [SensorTag("tag-1", "foo"), SensorTag("tag-2", "foo")],
                "train_start_date": "2016-01-01T00:00:00Z",
                "train_end_date": "2016-01-05T00:00:00Z",
            },
            "model": "sklearn.linear_model.LinearRegression",
        },
        project_name="test-project",
    )


@pytest.mark.parametrize(
    "machines,target,expected",
    [
        # Two machines, no target, should give two machines
        ([_machine("t1"), _machine("t2")], None, [_machine("t1"), _machine("t2")]),
        # One machine target should filter down to that machine
        ([_machine("t1"), _machine("t2")], "t2", [_machine("t2")]),
        # Target which doesn't match any machines raises error
        ([_machine("t1"), _machine("t2")], "t3", ValueError),
    ],
)
def test_client_machine_filtering(
    machines: typing.List[Machine],
    target: typing.Optional[str],
    expected: typing.List[Machine],
):
    if not isinstance(expected, list):
        with pytest.raises(ValueError):
            Client._filter_machines(machines, target)
    else:
        filtered_machines = Client._filter_machines(machines, target)
        assert (
            expected == filtered_machines
        ), f"Not equal: {expected} \n----\n {filtered_machines}"


def test_exponential_sleep_time(caplog, gordo_project, ml_server):

    start, end = (
        isoparse("2016-01-01T00:00:00+00:00"),
        isoparse("2016-01-01T12:00:00+00:00"),
    )

    with caplog.at_level(logging.CRITICAL):
        with patch("gordo.client.client.sleep", return_value=None) as time_sleep:
            client = Client(project=gordo_project)

            client._send_prediction_request(
                X=pd.DataFrame([123]),
                y=None,
                chunk=slice(0, 1),
                machine=_machine("t1"),
                start=start,
                end=end,
            )

            expected_calls = [call(8), call(16), call(32), call(64), call(128)]
            time_sleep.assert_has_calls(expected_calls)


def test__handle_response_errors():
    """
    Test expected error raising from gordo.client.io._handle_response
    """
    resp = requests.Response()
    resp.status_code = 422
    with pytest.raises(HttpUnprocessableEntity):
        _handle_response(resp)

    resp = requests.Response()
    resp.status_code = 403
    with pytest.raises(BadRequest):
        _handle_response(resp)

    resp = requests.Response()
    resp.status_code = 502
    with pytest.raises(IOError):
        _handle_response(resp)


@pytest.mark.parametrize("revision_specified", [True, False])
def test_client_set_revision(
    ml_server, gordo_project, gordo_revision, revision_specified
):
    """
    Client will auto-set to latest revision by default, else provide the requested revision
    """

    client = Client(
        project=gordo_project, revision=gordo_revision if revision_specified else None
    )
    assert client.revision == gordo_revision
    assert client.session.headers["revision"] == gordo_revision


def test_client_set_revision_error(ml_server, gordo_project):
    """
    Client will raise an error if asking for a revision that doesn't exist
    """
    with pytest.raises(LookupError):
        Client(project=gordo_project, revision="does-not-exist")


def test_client_auto_update_revision(ml_server, gordo_project, gordo_revision):
    """
    Given a client starts with a revision which is outdated, it will automatically update
    itself to match the latest being served.
    """
    client = Client(project=gordo_project)
    assert client.revision == gordo_revision  # by default it figures out the latest.

    # Abuse the private variable to change it to something else.
    client.session.headers["revision"] = "bad-revision"
    client._revision = "bad-revision"
    assert client.revision == "bad-revision"

    # Contacting the server with that revision will make the client update its revision
    with patch.object(client, "get_metadata") as get_metadata:
        client.get_machines()
        assert client.revision == gordo_revision
        assert client.session.headers["revision"] == gordo_revision

        # It should also make a call to update the metadata
        assert get_metadata.called_once()

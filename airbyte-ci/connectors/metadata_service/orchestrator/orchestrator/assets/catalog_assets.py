import yaml
import pandas as pd
import json
import requests
import pandas as pd
from metadata_service.models.generated.ConnectorMetadataDefinitionV0 import ConnectorMetadataDefinitionV0

from dagster import MetadataValue, Output, asset, OpExecutionContext
from jinja2 import Environment, PackageLoader

def render_connector_catalog_locations_html(destinations_table, sources_table):
    env = Environment(loader=PackageLoader("orchestrator", "templates"))
    template = env.get_template("connector_catalog_locations.html")
    return template.render(destinations_table=destinations_table, sources_table=sources_table)

def render_connector_catalog_locations_markdown(destinations_markdown, sources_markdown):
    env = Environment(loader=PackageLoader("orchestrator", "templates"))
    template = env.get_template("connector_catalog_locations.md")
    return template.render(destinations_markdown=destinations_markdown, sources_markdown=sources_markdown)


OSS_SUFFIX = "_oss"
CLOUD_SUFFIX = "_cloud"

def load_json_from_file(path):
    with open(path) as f:
        return json.load(f)

def OutputDataFrame(result_df):
    return Output(result_df, metadata={"count": len(result_df), "preview": MetadataValue.md(result_df.to_markdown())})

def get_primary_catalog_suffix(merged_df):
    cloud_only = merged_df["_merge"] == "right_only"
    primary_suffix = CLOUD_SUFFIX if cloud_only else OSS_SUFFIX
    secondary_suffix = OSS_SUFFIX if cloud_only else CLOUD_SUFFIX
    return primary_suffix, secondary_suffix

def get_field_with_fallback(merged_df, field):
    primary_suffix, secondary_suffix = get_primary_catalog_suffix(merged_df)

    primary_field = field + primary_suffix
    secondary_field = field + secondary_suffix

    secondary_value = merged_df.get(secondary_field)
    return merged_df.get(primary_field, default=secondary_value)

def compute_catalog_overrides(merged_df):
    cloud_only = merged_df["_merge"] == "right_only";
    oss_only = merged_df["_merge"] == "left_only";

    catalogs = {
        "oss": {
            "enabled": not cloud_only,
        },
        "cloud": {
            "enabled": not oss_only,
        }
    }

    # find the difference between the two catalogs
    if cloud_only or oss_only:
        return catalogs

    allowed_overrides = [
        "name",
        "dockerRepository",
        "dockerImageTag",
        "supportsDbt",
        "supportsNormalization",
        "license",
        "supportUrl",
        "sourceType",
        "allowedHosts",
        "normalizationConfig",
        "suggestedStreams",
        "resourceRequirements",
    ]

    # check if the columns are the same
    # TODO refactor this to handle cloud only
    for override_col in allowed_overrides:
        oss_col = override_col + OSS_SUFFIX
        cloud_col = override_col + CLOUD_SUFFIX

        cloud_value = merged_df.get(cloud_col)
        oss_value = merged_df.get(oss_col)

        # if the columns are different, add the cloud value to the overrides
        # TODO do a deep comparison
        if cloud_value and oss_value != cloud_value:
            catalogs["cloud"][override_col] = merged_df.get(cloud_col)

    return catalogs;


def merge_into_metadata_definitions(id_field, connector_type, oss_connector_df, cloud_connector_df) -> pd.Series:
    merged_connectors = pd.merge(oss_connector_df, cloud_connector_df, on=id_field, how='outer', suffixes=(OSS_SUFFIX, CLOUD_SUFFIX), indicator=True)
    sanitized_connectors = merged_connectors.where(pd.notnull(merged_connectors), None)
    def build_metadata(merged_df):
        raw_data = {
            "name": get_field_with_fallback(merged_df, "name"),
            "definitionId": merged_df[id_field],
            "connectorType": connector_type,
            "dockerRepository": get_field_with_fallback(merged_df, "dockerRepository"),
            "githubIssueLabel": get_field_with_fallback(merged_df, "dockerRepository").replace("airbyte/", ""),
            "dockerImageTag": get_field_with_fallback(merged_df, "dockerImageTag"),
            "icon": get_field_with_fallback(merged_df, "icon"),
            "supportUrl": get_field_with_fallback(merged_df, "documentationUrl"),
            "sourceType": get_field_with_fallback(merged_df, "sourceType"),
            "releaseStage": get_field_with_fallback(merged_df, "releaseStage"),
            "license": "MIT",

            "supportsDbt": get_field_with_fallback(merged_df, "supportsDbt"),
            "supportsNormalization": get_field_with_fallback(merged_df, "supportsNormalization"),
            "allowedHosts": get_field_with_fallback(merged_df, "allowedHosts"),
            "normalizationConfig": get_field_with_fallback(merged_df, "normalizationConfig"),
            "suggestedStreams": get_field_with_fallback(merged_df, "suggestedStreams"),
            "resourceRequirements": get_field_with_fallback(merged_df, "resourceRequirements"),
        }

        # remove none values
        data = {k: v for k, v in raw_data.items() if v is not None}

        metadata = {
            "metadataSpecVersion": "1.0",
            "data": data
        }

        catalogs = compute_catalog_overrides(merged_df)
        metadata["data"]["catalogs"] = catalogs

        return metadata

    metadata_list = [build_metadata(merged_df) for _, merged_df in sanitized_connectors.iterrows()]

    return metadata_list

def validate_metadata(metadata):
    try:
        ConnectorMetadataDefinitionV0.parse_obj(metadata)
        return True, None
    except Exception as e:
        return False, str(e)

@asset
def valid_metadata_list(metadata_definitions):
    result = []

    for metadata in metadata_definitions:
        valid, error_msg = validate_metadata(metadata)
        result.append({
            'definitionId': metadata["data"]['definitionId'],
            'name': metadata["data"]['name'],
            'dockerRepository': metadata["data"]['dockerRepository'],
            'is_metadata_valid': valid,
            'error_msg': error_msg
        })

    result_df = pd.DataFrame(result)

    return Output(result_df, metadata={"count": len(result_df), "preview": MetadataValue.md(result_df.to_markdown())})

@asset(required_resource_keys={"metadata_file_directory"})
def write_metadata_definitions_to_filesystem(context, metadata_definitions):
    files = []
    for metadata in metadata_definitions:
        connector_dir_name = metadata["data"]["dockerRepository"].replace("airbyte/", "")
        definitionId = metadata["data"]["definitionId"]

        key = f"{connector_dir_name}-{definitionId}"

        yaml_string = yaml.dump(metadata)

        file = context.resources.metadata_file_directory.write_data(yaml_string.encode(), ext="yaml", key=key)
        files.append(file)

    file_paths = [file.path for file in files]
    file_paths_str = "\n".join(file_paths)

    return Output(files, metadata={"count": len(files), "file_paths": file_paths_str})

@asset
def metadata_definitions(context, cloud_sources_dataframe, cloud_destinations_dataframe, oss_sources_dataframe, oss_destinations_dataframe):
    sources_metadata_list = merge_into_metadata_definitions("sourceDefinitionId", "source", oss_sources_dataframe, cloud_sources_dataframe)
    destinations_metadata_list = merge_into_metadata_definitions("destinationDefinitionId", "destination", oss_destinations_dataframe, cloud_destinations_dataframe)
    all_definitions = sources_metadata_list + destinations_metadata_list;
    context.log.info(f"Found {len(all_definitions)} metadata definitions")
    return Output(all_definitions, metadata={"count": len(all_definitions)})


@asset(required_resource_keys={"catalog_report_directory_manager"})
def connector_catalog_location_html(context, all_destinations_dataframe, all_sources_dataframe):
    """
    Generate an HTML report of the connector catalog locations.
    """

    columns_to_show = ["dockerRepository", "dockerImageTag", "is_oss", "is_cloud", "is_source_controlled", "is_spec_cached", "is_metadata_valid"]

    # convert true and false to checkmarks and x's
    all_sources_dataframe.replace({True: "✅", False: "❌"}, inplace=True)
    all_destinations_dataframe.replace({True: "✅", False: "❌"}, inplace=True)

    html = render_connector_catalog_locations_html(
        destinations_table=all_destinations_dataframe[columns_to_show].to_html(),
        sources_table=all_sources_dataframe[columns_to_show].to_html(),
    )

    catalog_report_directory_manager = context.resources.catalog_report_directory_manager
    file_handle = catalog_report_directory_manager.write_data(html.encode(), ext="html", key="connector_catalog_locations")

    metadata = {
        "preview": html,
        "gcs_path": MetadataValue.url(file_handle.gcs_path),
    }

    return Output(metadata=metadata, value=file_handle)


@asset(required_resource_keys={"catalog_report_directory_manager"})
def connector_catalog_location_markdown(context, all_destinations_dataframe, all_sources_dataframe):
    """
    Generate a markdown report of the connector catalog locations.
    """

    columns_to_show = ["dockerRepository", "dockerImageTag", "is_oss", "is_cloud", "is_source_controlled", "is_spec_cached", "is_metadata_valid"]

    # convert true and false to checkmarks and x's
    all_sources_dataframe.replace({True: "✅", False: "❌"}, inplace=True)
    all_destinations_dataframe.replace({True: "✅", False: "❌"}, inplace=True)

    markdown = render_connector_catalog_locations_markdown(
        destinations_markdown=all_destinations_dataframe[columns_to_show].to_markdown(),
        sources_markdown=all_sources_dataframe[columns_to_show].to_markdown(),
    )

    catalog_report_directory_manager = context.resources.catalog_report_directory_manager
    file_handle = catalog_report_directory_manager.write_data(markdown.encode(), ext="md", key="connector_catalog_locations")

    metadata = {
        "preview": MetadataValue.md(markdown),
        "gcs_path": MetadataValue.url(file_handle.gcs_path),
    }
    return Output(metadata=metadata, value=file_handle)

# TODO
# - add  column for whether the connector is source controlled
# - refactor so that the source and destination catalogs are merged into a single dataframe early on
# - refactor so we are importing a common dataclass
# - check which specs are available
# lets make sure markdown is still working
# then lets get specs all at once
# then lets hoise the merge
# move metadata to its own file

def is_spec_cached(dockerRepository, dockerImageTag):
    url = f"https://storage.googleapis.com/io-airbyte-cloud-spec-cache/specs/{dockerRepository}/{dockerImageTag}/spec.json"
    response = requests.head(url)
    return response.status_code == 200

def augment_and_normalize_connector_dataframes(cloud_df, oss_df, primaryKey, connector_type, valid_metadata_list, source_controlled_connectors):
        # Add a column 'is_cloud' to indicate if an image/version pair is in the cloud catalog
    cloud_df["is_cloud"] = True

    # Add a column 'is_oss' to indicate if an image/version pair is in the oss catalog
    oss_df["is_oss"] = True

    composite_key = [primaryKey, "dockerRepository", "dockerImageTag"]

    # Merge the two catalogs on the 'image' and 'version' columns, keeping only the unique pairs
    total_catalog = pd.merge(
        cloud_df, oss_df, how="outer", on=composite_key
    ).drop_duplicates(subset=composite_key)

    merged_catalog = pd.merge(total_catalog, valid_metadata_list[["definitionId", "is_metadata_valid"]], left_on=primaryKey, right_on="definitionId", how="left")

    # Replace NaN values in the 'is_cloud' and 'is_oss' columns with False
    merged_catalog[["is_cloud", "is_oss"]] = merged_catalog[["is_cloud", "is_oss"]].fillna(False)

    # Set connectorType to 'source' or 'destination'
    merged_catalog["connector_type"] = connector_type

    is_source_controlled = lambda x: x.lstrip("airbyte/") in source_controlled_connectors
    merged_catalog['is_source_controlled'] = merged_catalog['dockerRepository'].apply(is_source_controlled)
    merged_catalog['is_spec_cached'] = merged_catalog.apply(lambda x: is_spec_cached(x['dockerRepository'], x['dockerImageTag']), axis=1)

    return merged_catalog

@asset
def all_destinations_dataframe(cloud_destinations_dataframe, oss_destinations_dataframe, source_controlled_connectors, valid_metadata_list) -> pd.DataFrame:
    """
    Merge the cloud and oss destinations catalogs into a single dataframe.
    """

    return augment_and_normalize_connector_dataframes(
        cloud_df=cloud_destinations_dataframe,
        oss_df=oss_destinations_dataframe,
        primaryKey="destinationDefinitionId",
        connector_type="destination",
        valid_metadata_list=valid_metadata_list,
        source_controlled_connectors=source_controlled_connectors
    )


@asset
def all_sources_dataframe(cloud_sources_dataframe, oss_sources_dataframe, source_controlled_connectors, valid_metadata_list) -> pd.DataFrame:
    """
    Merge the cloud and oss source catalogs into a single dataframe.
    """
    return augment_and_normalize_connector_dataframes(
        cloud_df=cloud_sources_dataframe,
        oss_df=oss_sources_dataframe,
        primaryKey="sourceDefinitionId",
        connector_type="source",
        valid_metadata_list=valid_metadata_list,
        source_controlled_connectors=source_controlled_connectors
    )


@asset
def cloud_sources_dataframe(latest_cloud_catalog_dict: dict):
    sources = latest_cloud_catalog_dict["sources"]
    return OutputDataFrame(pd.DataFrame(sources))


@asset
def oss_sources_dataframe(latest_oss_catalog_dict: dict):
    sources = latest_oss_catalog_dict["sources"]
    return OutputDataFrame(pd.DataFrame(sources))


@asset
def cloud_destinations_dataframe(latest_cloud_catalog_dict: dict):
    destinations = latest_cloud_catalog_dict["destinations"]
    return OutputDataFrame(pd.DataFrame(destinations))


@asset
def oss_destinations_dataframe(latest_oss_catalog_dict: dict):
    destinations = latest_oss_catalog_dict["destinations"]
    return OutputDataFrame(pd.DataFrame(destinations))


@asset(required_resource_keys={"latest_cloud_catalog_gcs_file"})
def latest_cloud_catalog_dict(context: OpExecutionContext) -> dict:
    oss_catalog_file = context.resources.latest_cloud_catalog_gcs_file
    json_string = oss_catalog_file.download_as_string().decode("utf-8")
    oss_catalog_dict = json.loads(json_string)
    return oss_catalog_dict


@asset(required_resource_keys={"latest_oss_catalog_gcs_file"})
def latest_oss_catalog_dict(context: OpExecutionContext) -> dict:
    oss_catalog_file = context.resources.latest_oss_catalog_gcs_file
    json_string = oss_catalog_file.download_as_string().decode("utf-8")
    oss_catalog_dict = json.loads(json_string)
    return oss_catalog_dict

import argparse
import dxpy as dx
import json
import re

from collections import defaultdict
from packaging.version import Version, parse


def parse_args(argv=None) -> argparse.Namespace:
    """
    Parse command line arguments

    Returns
    -------
    args : Namespace
        Namespace of passed command line argument inputs
    """
    # Command line args set up for running tests
    parser = argparse.ArgumentParser(
        description='Config files to run tests for'
    )

    parser.add_argument(
        '-i',
        '--input_config',
        type=str,
        help="Config JSON file which was changed in PR"
    )

    parser.add_argument(
        '-f',
        '--file_id',
        nargs='?',
        type=str,
        help='File ID of changed config file in DNAnexus'
    )

    return parser.parse_args()


class DXManage():
    """
    Methods for generic handling of dx related things
    """
    def __init__(self, args) -> None:
        self.args = args


    @staticmethod
    def read_in_json(config_file) -> dict:
        """
        Read in config JSON file to a dict

        Parameters
        ----------
        config_file : str
            name of JSON config file to read in

        Returns
        -------
        config_dict: dict
            the content of the JSON converted to a dict
        """
        if not config_file.endswith('.json'):
            raise RuntimeError(
                'Error: invalid config file given - not a JSON file'
            )

        with open(config_file, 'r', encoding='utf8') as json_file:
            config_dict = json.load(json_file)

        return config_dict

    def get_json_configs_in_DNAnexus(self) -> dict:
        """
        Query path in DNAnexus for json config files for each assay, returning
        full data for all unarchived config files found

        ASSAY_CONFIG_PATH comes from the app config file sourced to the env.

        Returns
        -------
        list
            list of dicts of the json object for each config file found

        Raises
        ------
        AssertionError
            Raised when invalid project:path structure defined in app config
        AssertionError
            Raised when no config files found at the given path
        """
        config_path = (
            "project-Fkb6Gkj433GVVvj73J7x8KbV:/dynamic_files/"
            "eggd_conductor_configs/assay_configs/"
        )

        project, path = config_path.split(':')

        files = list(dx.find_data_objects(
            name="*.json",
            name_mode='glob',
            project=project,
            folder=path,
            describe=True
        ))

        # sense check we find config files
        assert files, print(
            f"No config files found in given path: {project}:{path}"
        )

        files_ids='\n\t'.join([
            f"{x['describe']['name']} ({x['id']} - "
            f"{x['describe']['archivalState']})" for x in files])
        print(f"\nAssay config files found:\n\t{files_ids}")

        all_configs = []
        for file in files:
            if file['describe']['archivalState'] == 'live':
                config_data = json.loads(
                    dx.bindings.dxfile.DXFile(
                        project=file['project'], dxid=file['id']).read())

                # add file ID as field into the config file
                config_data['file_id'] = file['id']
                all_configs.append(config_data)
            else:
                print(
                    "Config file not in live state - will not be used:"
                    f"{file['describe']['name']} ({file['id']}"
                )

        return all_configs


    @staticmethod
    def filter_highest_config_version(all_configs) -> dict:
        """
        Filters all configs found from get_json_configs() to retain highest
        version for each assay code to use for analysis.

        Assay codes are expected to be either a single code in the 'assay_code'
        field in the config file, or a '|' separated string of multiple.

        This keeps the highest version of each assay code, factoring in where
        one assay code may be a subset of another with a higher version, and
        only keeping the latter, i.e.
        {'EGG2': 1.0.0, 'EGG2|LAB123': 1.1.0} -> {'EGG2|LAB123': 1.1.0}

        Parameters
        ----------
        all_configs : list
            list of dicts of the json object for each config file
            found, returned from get_json_configs()

        Returns
        -------
        dict
            mapping of assay_code to full config data for the highest
            version config file for each assay_code

        Raises
        ------
        AssertionError
            Raised when config file has missing assay_code or version field
        """
        # filter all config files to just get full config data for the
        # highest version of each full assay code
        print("\nFiltering config files from DNAnexus for highest versions")
        highest_version_config_data = {}

        for config in all_configs:
            current_config_code = config.get('assay_code')
            current_config_ver = config.get('version')

            # sense check config file has code and version fields
            assert current_config_code and current_config_ver, print(
                f"Config file missing assay_code and/or version field!"
                f"File ID: {config['file_id']}"
            )

            # get highest stored version of config file for current code
            # we have found so far
            highest_version = highest_version_config_data.get(
                current_config_code, {}).get('version', '0')

            if Version(current_config_ver) > Version(highest_version):
                # higher version than stored one for same code => replace
                highest_version_config_data[current_config_code] = config

        # build simple dict of assay_code : version
        all_assay_codes = {
            x['assay_code']: x['version']
            for x in highest_version_config_data.values()
        }

        # get unique list of single codes from all assay codes, split on '|'
        # i.e. ['EGG1', 'EGG2', 'EGG2|LAB123'] -> ['EGG1', 'EGG2', 'LAB123']
        uniq_codes = [
            x.split('|') for x in all_assay_codes.keys()]
        uniq_codes = list(set([
            code for split_codes in uniq_codes for code in split_codes]))

        print(
            "\nUnique assay codes parsed from all config "
            f"assay_code fields {uniq_codes}\n"
        )

        # final dict of config files to use as assay_code : config data
        configs_to_use = {}

        # for each single assay code, find the highest version config file
        # that code is present in (i.e. {'EGG2': 1.0.0, 'EGG2|LAB123': 1.1.0}
        # would result in EGG2 -> {'EGG2|LAB123': 1.1.0})
        for uniq_code in uniq_codes:
            matches = {}
            for full_code in all_assay_codes.keys():
                if uniq_code in full_code.split('|'):
                    # this single assay code is in the full assay code
                    # parsed from config, add match as 'assay_code': 'version'
                    matches[full_code] =  all_assay_codes[full_code]

            # check we don't have 2 matches with the same version as we
            # can't tell which to use, i.e. EGG2 : 1.0.0 & EGG2|LAB123 : 1.0.0
            assert sorted(list(matches.values())) == \
                sorted(list(set(matches.values()))), print(
                f"More than one version of config file found for a single "
                f"assay code!\n\t{matches}"
            )

            # for this unique code, select the full assay code with the highest
            # version this one was found in using packaging.version.parse, and
            # then select the full config file data for it
            full_code_to_use = max(matches, key=parse)
            configs_to_use[
                full_code_to_use] = highest_version_config_data[full_code_to_use]

        # add to log record of highest version of each config found
        usable_configs = '\n\t'.join(
            [f"{k} ({v['version']}): {v['file_id']}"
            for k, v in configs_to_use.items()]
        )

        print(
            "\nHighest versions of assay configs found to use:"
            f"\n\t{usable_configs}\n"
        )

        return configs_to_use

    def match_updated_config_to_prod_config(
        self, updated_config, updated_config_id, prod_configs
    ):
        """
        For the config file which has been updated, get info on the
        highest related version in 001_Reference

        Parameters
        ----------
        updated_config : dict
            the config file that was updated in the PR as a dict
        updated_config_id: str
            DNAnexus file ID of updated config
        prod_configs : dict
            dict where keys are unique assay codes and values are full
            config as a dict

        Returns
        -------
        changed_config_to_prod : dict
            dict with info about the changed config and the related prod
            config
        """
        changed_config_to_prod = defaultdict(dict)
        prod_config_assay_codes = sorted([
            x.get('assay_code') for x in prod_configs.values()
        ])

        # Get assay code and version info from updated config
        updated_config_code = updated_config.get('assay_code')
        updated_config_version = updated_config.get('version')

        config_matches = {}
        for prod_code in prod_config_assay_codes:
            # find all prod config files that match the updated config assay
            # code
            if re.search(prod_code, updated_config_code, re.IGNORECASE):
                config_matches[prod_code] = prod_configs[prod_code]['version']

        if config_matches:
            # Get highest version of prod config files that matches the updated
            # config
            highest_ver_config = max(config_matches.values(), key=parse)

            latest_config_key = list(config_matches)[
                list(config_matches.values()).index(highest_ver_config)
            ]

            # Add version and file ID of the config file updated in the PR
            changed_config_to_prod['updated']['version'] = updated_config_version
            changed_config_to_prod['updated']['file_id'] = updated_config_id

            # Add version and file ID of the related prod config file
            changed_config_to_prod['prod']['version'] = highest_ver_config
            changed_config_to_prod['prod']['file_id'] = prod_configs[
                latest_config_key
            ]['file_id']

        return changed_config_to_prod


def main():
    args = parse_args()
    dx_manage = DXManage(args)

    changed_config_dict = dx_manage.read_in_json(args.input_config)

    config_data = dx_manage.get_json_configs_in_DNAnexus()
    configs_to_use = dx_manage.filter_highest_config_version(config_data)
    dx_manage.match_updated_config_to_prod_config(
        changed_config_dict, configs_to_use, args.file_id
    )

if __name__ == '__main__':
    main()

import requests
import re
import os
import glob
from lxml import etree
import lxml.html
import subprocess as sp
from azure.storage.blob import BlobClient


def my_print(cmd):
    print('== ' + cmd + ' ==\n')


def print_check(cmd):
    my_print(cmd)
    sp.check_call(cmd, shell=True)


class PyPIClient:
    def __init__(self, host="https://pypi.org", package_name='', track_config='',
                 readme_link='', rm_link='', cli_version=''):
        self._host = host
        self._session = requests.Session()
        self._package_name = package_name
        self.version_date_dict = {}
        self.whether_track2 = None  # whether published track2 to pypi
        self.track1_ga = 'NO'
        self.track1_latest = 'NA'
        self.track2_ga = 'NO'
        self.track2_latest = 'NA'
        self.pypi_link = 'NA'
        self.track_config = track_config
        self.readme_link = readme_link
        self.rm_link = rm_link
        self.cli_version = cli_version
        self.bot_warning = ''

    def get_package_name(self):
        return self._package_name

    def project_html(self):
        self.pypi_link = "{host}/pypi/{project_name}".format(
            host=self._host,
            project_name=self._package_name
        )
        response = self._session.get(self.pypi_link + "/#history")

        return response

    def get_release_info(self, response, xpath, type):
        DATE_DICT = {'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04', 'May': '05', 'Jun': '06',
                     'Jul': '07', 'Aug': '08', 'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12', }
        text = response.text
        parse_text = lxml.etree.HTML(text)
        release_info = parse_text.xpath(xpath)
        strip_info = []
        for info in release_info:
            info = info.strip()
            if type == 'date':
                info = info.replace(',', '').split(' ')
                info = info[2] + '/' + DATE_DICT[info[0]] + '/' + info[1]
            if not len(info) == 0:
                strip_info.append(info)

        return strip_info

    def get_release_dict(self, response):
        version_list = self.get_release_info(response, xpath='//p[@class="release__version"]/text()', type='version')
        self.version_handler(version_list)
        data_list = self.get_release_info(response, xpath='//p[@class="release__version-date"]/time/text()',
                                          type='date')
        self.version_date_dict = dict(zip(version_list, data_list))
        self.version_date_dict['NA'] = 'NA'

    def write_to_list(self):
        response = self.project_html()
        if 199 < response.status_code < 400:
            self.get_release_dict(response)
            self.bot_analysis()
            return '{},{},{},{},{},{},{},{},{},{},{},{}\n'.format(self._package_name,
                                                                  self.pypi_link,
                                                                  self.track1_latest,
                                                                  self.version_date_dict[self.track1_latest],
                                                                  self.track1_ga,
                                                                  self.track2_latest,
                                                                  self.track2_ga,
                                                                  self.version_date_dict[self.track2_latest],
                                                                  self.cli_version,
                                                                  self.track_config,
                                                                  self.bot_warning,
                                                                  self.rm_link)
        else:
            self.pypi_link = 'NA'
        return

    def version_handler(self, version_list):
        # Scenario 1
        # rule 1: this package have track2 version
        # rule 2: check whether CLI is using this package
        # rule 3: by comparing the versions of CLI and package, we can judge whether cli is using track1 or 2
        # rule 4: judge whether track1 is exist
        # rule 5: whether track1 is GA
        # rule 6: whether track2 is GA
        # Scenario 2
        # rule 7: this package doesn't have track2 version
        # rule 8: check whether CLI is using this package
        # rule 9: whether track1 is GA
        ga_re = re.compile(r'[A-Za-z]')
        version_index = 0
        versions = list(reversed(version_list))
        for version in versions:
            if 'b1' in version and self.whether_track2 is None:
                self.whether_track2 = version
                if self.cli_version != 'NA':
                    if int(self.cli_version.split('.')[0]) >= int(version.split('.')[0]):
                        self.cli_version = 'track2_' + self.cli_version
                    else:
                        self.cli_version = 'track1_' + self.cli_version
                if version_index != 0:
                    self.track1_latest = versions[version_index - 1]
                self.track2_latest = versions[-1]
                if not re.findall(ga_re, self.track1_latest) and len(self.track1_latest) != 0 and int(
                        self.track1_latest.split('.')[0]) > 0:
                    self.track1_ga = 'YES'
                if not re.findall(ga_re, self.track2_latest):
                    self.track2_ga = 'YES'
                break
            version_index += 1
        if self.whether_track2 is None:
            if self.cli_version != 'NA':
                self.cli_version = 'track1_' + self.cli_version
            self.track1_latest = versions[-1]
            if not re.findall(ga_re, self.track1_latest) and len(self.track1_latest) != 0 and int(
                    self.track1_latest.split('.')[0]) > 0:
                self.track1_ga = 'YES'

    def bot_analysis(self):
        # rule 1: readme.python.md must exist
        # rule 2: track1 config must be deleted if azure-cli doesn't use track1
        # rule 3: track2 config must be added if track2 package has been published to pypi
        if self.readme_link == 'NA':
            self.bot_warning += 'The readme.python.md has not been created.  '
        if self.cli_version != 'NA':
            cli_version = int(self.cli_version.split('_')[1].split('.')[0])
            if self.whether_track2 is not None:
                whether_track2 = int(self.whether_track2.split('.')[0])
                if cli_version >= whether_track2 and self.track_config == 'both':
                    self.bot_warning += 'The cli using track2 now but readme.python still have track1 config.'
        if self.whether_track2 and self.track_config == 'track1':
            self.bot_warning += 'Need to add track2 config.'


def sdk_info_from_pypi(sdk_info, cli_dependency):
    all_sdk_status = []
    for package in sdk_info:
        if ',' in package:
            package = package.split(',')
            sdk_name = package[0].strip()
            if sdk_name in cli_dependency.keys():
                cli_version = cli_dependency[sdk_name]
            else:
                cli_version = 'NA'
            track_config = package[1].strip()
            readme_link = package[2].strip()
            rm_link = package[3].strip()
            pypi_ins = PyPIClient(package_name=sdk_name, track_config=track_config,
                                  readme_link=readme_link, rm_link=rm_link, cli_version=cli_version)
            text_to_write = pypi_ins.write_to_list()
            if pypi_ins.pypi_link != 'NA':
                all_sdk_status.append(text_to_write)

    my_print(f'total pypi package kinds: {len(all_sdk_status)}')
    return all_sdk_status


def write_to_csv(sdk_status_list, csv_name):
    with open(csv_name, 'w') as file_out:
        file_out.write('package name,'
                       'pypi link,'
                       'latest track1,'
                       'release date,'
                       'track1 GA,'
                       'latest track2,'
                       'track2 GA,'
                       'release date,'
                       'cli dependency,'
                       'readme config,'
                       'bot advice,'
                       'readme link\n')
        file_out.writelines(
            [package for package in sorted(sdk_status_list, key=lambda x: x.split(',')[10], reverse=True)])


def get_cli_dependency():
    CLI_URL = 'https://github.com/azure/azure-cli/blob/dev/src/azure-cli/setup.py'
    cli_lines = project_html(CLI_URL).xpath('//table[@class="highlight tab-size js-file-line-container"]//text()')
    cli_dependency = {}
    for line in cli_lines:
        if 'azure-mgmt-' in line:
            line = line[1:-1]
            if '==' in line:
                line = line.split('==')
                cli_dependency[line[0]] = line[1]
            elif '~=' in line:
                line = line.split('~=')
                cli_dependency[line[0]] = line[1]
    return cli_dependency


def project_html(url):
    response = requests.Session().get(url)
    response.encoding = 'gbk'
    text = response.text
    parse_result = lxml.etree.HTML(text)
    return parse_result


def read_file(file_name):
    with open(file_name, 'r', encoding='utf-8') as file_in:
        content = file_in.readlines()
    return content


def sdk_info_from_swagger():
    sdk_name_re = re.compile(r'azure-mgmt-[a-z]+-*([a-z])+')
    resource_manager = []
    SWAGGER_FOLDER = os.getenv('SWAGGER_REPO')
    readme_folders = glob.glob(f'{SWAGGER_FOLDER}/specification/*/resource-manager/readme.md')
    my_print(f'total readme folders: {len(readme_folders)}')

    for folder in readme_folders:
        track_config = 0
        package_name = ''
        folder = folder.replace('readme.md', '')
        readme_python = 'NA' if 'readme.python.md' not in os.listdir(folder) else f'{folder}/readme.python.md'
        readme_text = read_file(folder + 'readme.md')
        for line in readme_text:
            if line.find('azure-sdk-for-python-track2') > -1:
                track_config += 2
            elif line.find('azure-sdk-for-python') > -1:
                track_config += 1
            if readme_python == 'NA' and sdk_name_re.search(line) is not None and package_name == '':
                package_name = sdk_name_re.search(line).group()

        if readme_python != 'NA':
            readme_python_text = read_file(readme_python)
            for text in readme_python_text:
                if sdk_name_re.search(text) is not None:
                    package_name = sdk_name_re.search(text).group()

        TRACK_CONFIG = {0: 'NA', 1: 'track1', 2: 'track2', 3: 'both'}
        track_config = TRACK_CONFIG.get(track_config, 'Rule error')
        readme_html = folder.replace(SWAGGER_FOLDER, 'https://github.com/Azure/azure-rest-api-specs/tree/master')
        if package_name != '':
            resource_manager.append('{},{},{},{}\n'.format(package_name,
                                                           track_config,
                                                           readme_python,
                                                           readme_html))
        my_print(f'{folder} : {package_name}')

    my_print(f'total package kinds: {len(resource_manager)}')
    return resource_manager


def commit_to_github():
    print_check('git add .')
    print_check('git commit -m \"update excel\"')
    print_check('git push -f origin HEAD')


def upload_to_azure(out_file):
    # upload to storage account(it is created in advance)
    blob = BlobClient.from_connection_string(conn_str=os.getenv('CONN_STR'), container_name=os.getenv('FILE'),
                                             blob_name=out_file)
    with open(out_file, 'rb') as data:
        blob.upload_blob(data, overwrite=True)


def main():
    cli_dependency = get_cli_dependency()
    sdk_info = sdk_info_from_swagger()
    all_sdk_status = sdk_info_from_pypi(sdk_info, cli_dependency)

    OUT_FILE = 'release_sdk_status.csv'
    write_to_csv(all_sdk_status, OUT_FILE)
    commit_to_github()
    upload_to_azure(OUT_FILE)


if __name__ == '__main__':
    main()

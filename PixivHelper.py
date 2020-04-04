# -*- coding: utf-8 -*-
# pylint: disable=W0603

import codecs
import html
import json
import logging
import logging.handlers
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib
import zipfile
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

import imageio
import mechanize
from apng import APNG

import PixivConstant
from PixivImage import PixivImage

logger = None
_config = None
__re_manga_index = re.compile(r'_p(\d+)')
__badchars__ = re.compile(r'''
^$
|\?
|:
|<
|>
|\|
|\*
|\"
''', re.VERBOSE)


def set_config(config):
    global _config
    _config = config


def get_logger(level=logging.DEBUG):
    '''Set up logging'''
    global logger
    if logger is None:
        script_path = module_path()
        logger = logging.getLogger('PixivUtil' + PixivConstant.PIXIVUTIL_VERSION)
        logger.setLevel(level)
        __logHandler__ = logging.handlers.RotatingFileHandler(script_path + os.sep + PixivConstant.PIXIVUTIL_LOG_FILE,
                                                              maxBytes=PixivConstant.PIXIVUTIL_LOG_SIZE,
                                                              backupCount=PixivConstant.PIXIVUTIL_LOG_COUNT,
                                                              encoding="utf-8")
        __formatter__ = logging.Formatter(PixivConstant.PIXIVUTIL_LOG_FORMAT)
        __logHandler__.setFormatter(__formatter__)
        logger.addHandler(__logHandler__)
    return logger


def set_log_level(level):
    logger.info("Setting log level to: %s", level)
    get_logger(level).setLevel(level)


def sanitize_filename(name, rootDir=None):
    '''Replace reserved character/name with underscore (windows), rootDir is not sanitized.'''
    # get the absolute rootdir
    if rootDir is not None:
        rootDir = os.path.abspath(rootDir)

    # Unescape '&amp;', '&lt;', and '&gt;'
    name = html.unescape(name)

    name = __badchars__.sub("_", name)

    # Remove unicode control characters
    name = "".join(c for c in name if unicodedata.category(c) != "Cc")

    # Strip leading/trailing space for each directory
    # Issue #627: remove trailing '.'
    # Ensure Windows reserved filenames are prefixed with _
    stripped_name = list()
    for item in name.split(os.sep):
        if Path(item).is_reserved():
            item = '_' + item
        stripped_name.append(item.strip(" .\t\r\n"))
    name = os.sep.join(stripped_name)

    if platform.system() == 'Windows':
        # cut whole path to 255 char
        # TODO: check for Windows long path extensions being enabled
        if rootDir is not None:
            full_name = os.path.abspath(os.path.join(rootDir, name))
        else:
            full_name = os.path.abspath(name)
        if len(full_name) > 255:
            filename, extname = os.path.splitext(name)  # NOT full_name, to avoid clobbering paths
            # don't trim the extension
            name = filename[:255 - len(extname)] + extname
            if name == extname:  # we have no file name left
                raise OSError(None, "Path name too long", full_name, 0x000000A1)  # 0xA1 is "invalid path"
    else:
        # Unix: cut filename to <= 249 bytes
        # TODO: allow macOS higher limits, HFS+ allows 255 UTF-16 chars, and APFS 255 UTF-8 chars
        while len(name.encode('utf-8')) > 249:
            filename, extname = os.path.splitext(name)
            name = filename[:len(filename) - 1] + extname

    if rootDir is not None:
        name = os.path.abspath(os.path.join(rootDir, name))

    get_logger().debug("Sanitized Filename: %s", name)

    return name


# Issue #277: always replace '/' and '\' with '_' for %artist%, %title%, %searchTags%, %tags%, %works_tools%, and %original_artist%.
def replace_path_separator(s, replacement='_'):
    return s.replace('/', replacement).replace('\\', replacement)


def make_filename(nameFormat, imageInfo, artistInfo=None, tagsSeparator=' ', tagsLimit=-1, fileUrl='',
                  appendExtension=True, bookmark=False, searchTags=''):
    '''Build the filename from given info to the given format.'''
    if artistInfo is None:
        artistInfo = imageInfo.artist

    # Get the image extension
    fileUrl = os.path.basename(fileUrl)
    imageExtension = ""
    imageFile = fileUrl
    if fileUrl.find(".") > 0:
        splittedUrl = fileUrl.split('.')
        imageFile = splittedUrl[0]
        imageExtension = splittedUrl[1]
        imageExtension = imageExtension.split('?')[0]

    # artist related
    nameFormat = nameFormat.replace('%artist%', replace_path_separator(artistInfo.artistName))
    nameFormat = nameFormat.replace('%member_id%', str(artistInfo.artistId))
    nameFormat = nameFormat.replace('%member_token%', artistInfo.artistToken)

    # image related
    nameFormat = nameFormat.replace('%title%', replace_path_separator(imageInfo.imageTitle))
    nameFormat = nameFormat.replace('%image_id%', str(imageInfo.imageId))
    nameFormat = nameFormat.replace('%works_date%', imageInfo.worksDate)
    nameFormat = nameFormat.replace('%works_date_only%', imageInfo.worksDate.split(' ')[0])

    # formatted works date/time, ex. %works_date_fmt{%Y-%m-%d}%
    if nameFormat.find("%works_date_fmt") > -1:
        to_replace = re.findall("(%works_date_fmt{.*}%)", nameFormat)
        date_format = re.findall("{(.*)}", to_replace[0])
        nameFormat = nameFormat.replace(to_replace[0], imageInfo.worksDateDateTime.strftime(date_format[0]))

    nameFormat = nameFormat.replace('%works_res%', imageInfo.worksResolution)
    nameFormat = nameFormat.replace('%works_tools%', replace_path_separator(imageInfo.worksTools))
    nameFormat = nameFormat.replace('%urlFilename%', imageFile)
    nameFormat = nameFormat.replace('%searchTags%', replace_path_separator(searchTags))

    # date
    nameFormat = nameFormat.replace('%date%', date.today().strftime('%Y%m%d'))

    # formatted date/time, ex. %date_fmt{%Y-%m-%d}%
    if nameFormat.find("%date_fmt") > -1:
        to_replace2 = re.findall("(%date_fmt{.*}%)", nameFormat)
        date_format2 = re.findall("{(.*)}", to_replace2[0])
        nameFormat = nameFormat.replace(to_replace2[0], datetime.today().strftime(date_format2[0]))

    # get the page index & big mode if manga
    page_index = ''
    page_number = ''
    page_big = ''
    if imageInfo.imageMode == 'manga':
        idx = __re_manga_index.findall(fileUrl)
        if len(idx) > 0:
            page_index = idx[0]
            page_number = str(int(page_index) + 1)
            padding = len(str(imageInfo.imageCount)) or 1
            page_number = str(page_number)
            page_number = page_number.zfill(padding)
        if fileUrl.find('_big') > -1 or fileUrl.find('_m') <= -1:
            page_big = 'big'
    nameFormat = nameFormat.replace('%page_big%', page_big)
    nameFormat = nameFormat.replace('%page_index%', page_index)
    nameFormat = nameFormat.replace('%page_number%', page_number)

    if tagsSeparator == '%space%':
        tagsSeparator = ' '
    if tagsSeparator == '%ideo_space%':
        tagsSeparator = u'\u3000'

    if tagsLimit != -1:
        tagsLimit = tagsLimit if tagsLimit < len(imageInfo.imageTags) else len(imageInfo.imageTags)
        imageInfo.imageTags = imageInfo.imageTags[0:tagsLimit]
    tags = tagsSeparator.join(imageInfo.imageTags)
    r18Dir = ""
    if "R-18G" in imageInfo.imageTags:
        r18Dir = "R-18G"
    elif "R-18" in imageInfo.imageTags:
        r18Dir = "R-18"
    nameFormat = nameFormat.replace('%R-18%', r18Dir)
    nameFormat = nameFormat.replace('%tags%', replace_path_separator(tags))
    nameFormat = nameFormat.replace('&#039;', '\'')  # Yavos: added html-code for "'" - works only when ' is excluded from __badchars__

    if bookmark:  # from member bookmarks
        nameFormat = nameFormat.replace('%bookmark%', 'Bookmarks')
        nameFormat = nameFormat.replace('%original_member_id%', str(imageInfo.originalArtist.artistId))
        nameFormat = nameFormat.replace('%original_member_token%', imageInfo.originalArtist.artistToken)
        nameFormat = nameFormat.replace('%original_artist%', replace_path_separator(imageInfo.originalArtist.artistName))
    else:
        nameFormat = nameFormat.replace('%bookmark%', '')
        nameFormat = nameFormat.replace('%original_member_id%', str(artistInfo.artistId))
        nameFormat = nameFormat.replace('%original_member_token%', artistInfo.artistToken)
        nameFormat = nameFormat.replace('%original_artist%', replace_path_separator(artistInfo.artistName))

    if imageInfo.bookmark_count > 0:
        nameFormat = nameFormat.replace('%bookmark_count%', str(imageInfo.bookmark_count))
    else:
        nameFormat = nameFormat.replace('%bookmark_count%', '')

    if imageInfo.image_response_count > 0:
        nameFormat = nameFormat.replace('%image_response_count%', str(imageInfo.image_response_count))
    else:
        nameFormat = nameFormat.replace('%image_response_count%', '')

    # clean up double space
    while nameFormat.find('  ') > -1:
        nameFormat = nameFormat.replace('  ', ' ')

    if appendExtension:
        nameFormat = nameFormat.strip() + '.' + imageExtension

    return nameFormat.strip()


def safePrint(msg, newline=True):
    """Print empty string if UnicodeError raised."""
    for msgToken in msg.split(' '):
        try:
            print(msgToken, end=' ')
        except UnicodeError:
            print(('?' * len(msgToken)), end=' ')
    if newline:
        print("")


def set_console_title(title):
    if os.name == 'nt':
        subprocess.call('title' + ' ' + title, shell=True)
    else:
        sys.stdout.write("\x1b]2;" + title + "\x07")


def clearScreen():
    if os.name == 'nt':
        subprocess.call('cls', shell=True)
    else:
        subprocess.call('clear', shell=True)


def start_irfanview(dfilename, irfanViewPath, start_irfan_slide=False, start_irfan_view=False):
    print_and_log('info', 'starting IrfanView...')
    if os.path.exists(dfilename):
        ivpath = irfanViewPath + os.sep + 'i_view32.exe'  # get first part from config.ini
        ivpath = ivpath.replace('\\\\', '\\')
        ivpath = ivpath.replace('\\', os.sep)
        info = None
        if start_irfan_slide:
            info = subprocess.STARTUPINFO()
            info.dwFlags = 1
            info.wShowWindow = 6  # start minimized in background (6)
            ivcommand = ivpath + ' /slideshow=' + dfilename
            logger.info(ivcommand)
            subprocess.Popen(ivcommand)
        elif start_irfan_view:
            ivcommand = ivpath + ' /filelist=' + dfilename
            logger.info(ivcommand)
            subprocess.Popen(ivcommand, startupinfo=info)
    else:
        print_and_log('error', u'could not load' + dfilename)


def open_text_file(filename, mode='r', encoding='utf-8'):
    ''' taken from: http://www.velocityreviews.com/forums/t328920-remove-bom-from-string-read-from-utf-8-file.html'''
    hasBOM = False
    if os.path.isfile(filename):
        f = open(filename, 'rb')
        header = f.read(4)
        f.close()

        # Don't change this to a map, because it is ordered
        encodings = [(codecs.BOM_UTF32, 'utf-32'),
                     (codecs.BOM_UTF16, 'utf-16'),
                     (codecs.BOM_UTF8, 'utf-8')]

        for h, e in encodings:
            if header.startswith(h):
                encoding = e
                hasBOM = True
                break

    f = codecs.open(filename, mode, encoding)
    # Eat the byte order mark
    if hasBOM:
        f.read(1)
    return f


def create_avatar_filename(artistPage, targetDir):
    filename = ''
    image = PixivImage(parent=artistPage)
    # Download avatar using custom name, refer issue #174
    if len(_config.avatarNameFormat) > 0:
        filenameFormat = _config.avatarNameFormat
        filename = make_filename(filenameFormat, image,
                                 tagsSeparator=_config.tagsSeparator,
                                 tagsLimit=_config.tagsLimit,
                                 fileUrl=artistPage.artistAvatar,
                                 appendExtension=True)
        filename = sanitize_filename(filename, targetDir)
    else:
        # or as folder.jpg
        filenameFormat = _config.filenameFormat
        if filenameFormat.find(os.sep) == -1:
            filenameFormat = os.sep + filenameFormat
        filenameFormat = os.sep.join(filenameFormat.split(os.sep)[:-1])
        filename = make_filename(filenameFormat, image,
                                 tagsSeparator=_config.tagsSeparator,
                                 tagsLimit=_config.tagsLimit,
                                 fileUrl=artistPage.artistAvatar,
                                 appendExtension=False)
        filename = sanitize_filename(filename + os.sep + 'folder.jpg', targetDir)
    return filename


def create_bg_filename_from_avatar_filename(avatarFilename):
    filenames = avatarFilename.split(os.sep)
    filenames[-1] = "bg_" + filenames[-1]
    filename = os.sep.join(filenames)
    return filename


def we_are_frozen():
    """Returns whether we are frozen via py2exe.
        This will affect how we find out where we are located.
        Get actual script directory
        http://www.py2exe.org/index.cgi/WhereAmI"""

    return hasattr(sys, "frozen")


def module_path():
    """ This will get us the program's directory,
  even if we are frozen using py2exe"""

    if we_are_frozen():
        return os.path.dirname(sys.executable)

    return os.path.dirname(__file__)


def speed_in_str(totalSize, totalTime):
    if totalTime > 0:
        speed = totalSize / totalTime
        if speed < 1024:
            return "{0:.0f} B/s".format(speed)
        speed = speed / 1024
        if speed < 1024:
            return "{0:.2f} KiB/s".format(speed)
        speed = speed / 1024
        if speed < 1024:
            return "{0:.2f} MiB/s".format(speed)
        speed = speed / 1024
        return "{0:.2f} GiB/s".format(speed)
    else:
        return " infinity B/s"


def size_in_str(totalSize):
    totalSize = float(totalSize)
    if totalSize < 1024:
        return "{0:.0f} B".format(totalSize)
    totalSize = totalSize / 1024
    if totalSize < 1024:
        return "{0:.2f} KiB".format(totalSize)
    totalSize = totalSize / 1024
    if totalSize < 1024:
        return "{0:.2f} MiB".format(totalSize)
    totalSize = totalSize / 1024
    return "{0:.2f} GiB".format(totalSize)


def dump_html(filename, html_text):
    isDumpEnabled = True
    filename = sanitize_filename(filename)
    if _config is not None:
        isDumpEnabled = _config.enableDump
        if _config.enableDump:
            if len(_config.skipDumpFilter) > 0:
                matchResult = re.findall(_config.skipDumpFilter, filename)
                if matchResult is not None and len(matchResult) > 0:
                    isDumpEnabled = False

    if html_text is not None and len(html_text) == 0:
        print_and_log('info', 'Empty Html.')
        return ""

    if isDumpEnabled:
        if not isinstance(html_text, str):
            html_text = str(html_text)
        if isinstance(html_text, str):
            html_text = html_text.encode()
        try:
            dump = open(filename, 'wb')
            dump.write(html_text)
            dump.close()
            return filename
        except IOError as ex:
            print_and_log('error', str(ex))
        print_and_log("info", "Dump File created: {0}".format(filename))
    else:
        print_and_log('info', 'Dump not enabled.')
    return ""


def print_and_log(level, msg):
    if level == 'debug':
        get_logger().debug(msg)
    else:
        safePrint(msg)
        if level == 'info':
            get_logger().info(msg)
        elif level == 'warn':
            get_logger().warning(msg)
        elif level == 'error':
            get_logger().error(msg)
            get_logger().error(traceback.format_exc())


def have_strings(page, strings):
    for string in strings:
        pattern = re.compile(string)
        test_2 = pattern.findall(str(page))
        if len(test_2) > 0:
            if len(test_2[-1]) > 0:
                return True
    return False


def get_ids_from_csv(ids_str, sep=','):
    ids = list()
    ids_str = str(ids_str).split(sep)
    for id_str in ids_str:
        temp = id_str.strip()
        if len(temp) > 0:
            try:
                _id = int(temp)
                ids.append(_id)
            except ValueError:
                print_and_log('error', u"ID: {0} is not valid".format(id_str))
    if len(ids) > 1:
        print_and_log('info', u"Found {0} ids".format(len(ids)))
    return ids


def clear_all():
    all_vars = [var for var in globals() if (var[:2], var[-2:]) != ("__", "__") and var != "clear_all"]
    for var in all_vars:
        del globals()[var]


# # pylint: disable=W0612
# def unescape_charref(data, encoding):
#     ''' Replace default mechanize method in _html.py'''
#     try:
#         name, base = data, 10
#         if name.lower().startswith("x"):
#             name, base = name[1:], 16
#         try:
#             int(name, base)
#         except ValueError:
#             base = 16
#         uc = chr(int(name, base))
#         if encoding is None:
#             return uc

#         try:
#             repl = uc.encode(encoding)
#         except UnicodeError:
#             repl = "&#%s;" % data
#         return repl
#     except BaseException:
#         return data


def get_ugoira_size(ugoName):
    size = 0
    try:
        with zipfile.ZipFile(ugoName) as z:
            animJson = z.read("animation.json")
            size = json.loads(animJson)['zipSize']
            z.close()
    except zipfile.BadZipFile:
        print_and_log('error', u'Failed to read ugoira size from json data: {0}, using filesize.'.format(ugoName))
        size = os.path.getsize(ugoName)
    return size


def check_file_exists(overwrite, filename, file_size, old_size, backup_old_file):
    if not overwrite and int(file_size) == old_size:
        print_and_log('info', u"\tFile exist! (Identical Size)")
        return PixivConstant.PIXIVUTIL_SKIP_DUPLICATE
    # elif int(file_size) < old_size:
    #    printAndLog('info', "\tFile exist! (Local is larger)")
    #    return PixivConstant.PIXIVUTIL_SKIP_LOCAL_LARGER
    else:
        if backup_old_file:
            split_name = filename.rsplit(".", 1)
            new_name = filename + "." + str(int(time.time()))
            if len(split_name) == 2:
                new_name = split_name[0] + "." + str(int(time.time())) + "." + split_name[1]
            print_and_log('info', u"\t Found file with different file size, backing up to: " + new_name)
            os.rename(filename, new_name)
        else:
            print_and_log('info', u"\tFound file with different file size, removing old file (old: {0} vs new: {1})"
                          .format(old_size, file_size))
            os.remove(filename)
        return PixivConstant.PIXIVUTIL_OK


def print_delay(retryWait):
    repeat = range(1, retryWait)
    for t in repeat:
        print(t, end=' ')
        time.sleep(1)
    print('')


def create_custom_request(url, config, referer='https://www.pixiv.net', head=False):
    if config.useProxy:
        proxy = urllib.request.ProxyHandler(config.proxy)
        opener = urllib.request.build_opener(proxy)
        urllib.request.install_opener(opener)
    req = mechanize.Request(url)
    req.add_header('Referer', referer)
    print_and_log('info', u"Using Referer: " + str(referer))

    if head:
        req.get_method = lambda: 'HEAD'
    else:
        req.get_method = lambda: 'GET'

    return req


def makeSubdirs(filename):
    directory = os.path.dirname(filename)
    if not os.path.exists(directory) and len(directory) > 0:
        print_and_log('info', u'Creating directory: ' + directory)
        os.makedirs(directory)


def download_image(url, filename, res, file_size, overwrite):
    ''' Actual download, return the downloaded filesize and saved filename.'''
    start_time = datetime.now()

    # try to save to the given filename + .pixiv extension if possible
    try:
        makeSubdirs(filename)
        save = open(filename + '.pixiv', 'wb+', 4096)
    except IOError:
        print_and_log('error', u"Error at download_image(): Cannot save {0} to {1}: {2}".format(url, filename, sys.exc_info()))

        # get the actual server filename and use it as the filename for saving to current app dir
        filename = os.path.split(url)[1]
        filename = filename.split("?")[0]
        filename = sanitize_filename(filename)
        save = open(filename + '.pixiv', 'wb+', 4096)
        print_and_log('info', u'File is saved to ' + filename)

    # download the file
    prev = 0
    curr = 0
    print('{0:22} Bytes'.format(curr), end=' ')
    try:
        while True:
            save.write(res.read(PixivConstant.BUFFER_SIZE))
            curr = save.tell()
            print_progress(curr, file_size)

            # check if downloaded file is complete
            if file_size > 0 and curr == file_size:
                total_time = (datetime.now() - start_time).total_seconds()
                print(u' Completed in {0}s ({1})'.format(total_time, speed_in_str(file_size, total_time)))
                break

            elif curr == prev:  # no file size info
                total_time = (datetime.now() - start_time).total_seconds()
                print(u' Completed in {0}s ({1})'.format(total_time, speed_in_str(curr, total_time)))
                break

            prev = curr

    finally:
        if save is not None:
            save.close()

        completed = True
        if file_size > 0 and curr < file_size:
            # File size is known and downloaded file is smaller
            print_and_log('error', u'Downloaded file incomplete! {0:9} of {1:9} Bytes'.format(curr, file_size))
            print_and_log('error', u'Filename = ' + filename)
            print_and_log('error', u'URL      = {0}'.format(url))
            completed = False
        elif curr == 0:
            # No data received.
            print_and_log('error', u'No data received!')
            print_and_log('error', u'Filename = ' + filename)
            print_and_log('error', u'URL      = {0}'.format(url))
            completed = False

        if completed:
            if overwrite and os.path.exists(filename):
                os.remove(filename)
            os.rename(filename + '.pixiv', filename)
        else:
            os.remove(filename + '.pixiv')

        del save

    return (curr, filename)


def print_progress(curr, total):
    # [12345678901234567890]
    # [||||||||------------]
    animBarLen = 20

    if total > 0:
        complete = int((curr * animBarLen) / total)
        print(f"[{'|' * complete:{animBarLen}}] {size_in_str(curr)} of {size_in_str(total)}", end='\r')
    else:
        # indeterminite
        pos = curr % (animBarLen + 3)  # 3 corresponds to the length of the '|||' below
        anim = '.' * animBarLen + '|||' + '.' * animBarLen
        # Use nested replacement field to specify the precision value. This limits the maximum print
        # length of the progress bar. As pos changes, the starting print position of the anim string
        # also changes, thus producing the scrolling effect.
        print(f'[{anim[animBarLen + 3 - pos:]:.{animBarLen}}] {size_in_str(curr)}', end='\r')


def generate_search_tag_url(tags, page, title_caption, wild_card, oldest_first,
                            start_date=None, end_date=None, member_id=None,
                            r18mode=False, blt=0, type_data="a"):
    url = ""
    date_param = ""
    page_param = ""

    if start_date is not None:
        date_param = date_param + "&scd=" + start_date
    if end_date is not None:
        date_param = date_param + "&ecd=" + end_date
    if page is not None and int(page) > 1:
        page_param = "&p={0}".format(page)

    if member_id is not None:
        url = 'https://www.pixiv.net/member_illust.php?id=' + str(member_id) + '&tag=' + tags + '&p=' + str(page)
    else:
        root_url = 'https://www.pixiv.net/ajax/search/artworks'
        search_mode = ""
        if title_caption:
            search_mode = '&s_mode=s_tc'
            print(u"Using Title Match (s_tc)")
        elif wild_card:
            # partial match
            print(u"Using Partial Match (s_tag)")
        else:
            search_mode = '&s_mode=s_tag_full'
            print(u"Using Full Match (s_tag_full)")

        bookmark_limit_premium = ""
        if blt > 0:
            bookmark_limit_premium = f'&blt={blt}'

        if type_data == "i":
            type_data = "illust_and_ugoira"
        elif type_data == "m":
            type_data = "manga"
        else:
            type_data = "all"
        type_mode = f"&type={type_data}"

        url = f"{root_url}/{tags}?word={tags}{date_param}{page_param}{search_mode}{bookmark_limit_premium}{type_mode}"

    if r18mode:
        url = url + '&mode=r18'

    if oldest_first:
        url = url + '&order=date'
    # else:
    #    url = url + '&order=date_d'

    # encode to ascii
    # url = url.encode('iso_8859_1')

    return url


def write_url_in_description(image, blacklistRegex, filenamePattern):
    valid_url = list()
    if len(image.descriptionUrlList) > 0:
        # filter first
        if len(blacklistRegex) > 0:
            for link in image.descriptionUrlList:
                res = re.findall(blacklistRegex, link)
                if len(res) == 0:
                    valid_url.append(link)
        else:
            valid_url = image.descriptionUrlList

    # then write
    if len(valid_url) > 0:
        if len(filenamePattern) == 0:
            filenamePattern = "url_list_%Y%m%d"
        filename = date.today().strftime(filenamePattern) + ".txt"
        makeSubdirs(filename)
        info = codecs.open(filename, 'a', encoding='utf-8')
        info.write("#" + str(image.imageId) + "\r\n")
        for link in valid_url:
            info.write(link + "\r\n")
        info.close()


def ugoira2gif(ugoira_file, exportname, delete_ugoira, fmt='gif', image=None):
    print_and_log('info', 'processing ugoira to animated gif...')
    temp_folder = tempfile.mkdtemp()
    # imageio cannot handle utf-8 filename
    temp_name = temp_folder + os.sep + "temp.gif"

    with zipfile.ZipFile(ugoira_file) as f:
        f.extractall(temp_folder)

    filenames = os.listdir(temp_folder)
    filenames.remove('animation.json')
    anim_info = json.load(open(temp_folder + '/animation.json'))

    durations = []
    images = []
    for info in anim_info["frames"]:
        images.append(imageio.imread(temp_folder + os.sep + info["file"]))
        durations.append(float(info["delay"]) / 1000)

    kargs = {'duration': durations}
    imageio.mimsave(temp_name, images, fmt, **kargs)
    shutil.move(temp_name, exportname)
    print_and_log('info', 'ugoira exported to: ' + exportname)

    shutil.rmtree(temp_folder)
    if delete_ugoira:
        print_and_log('info', 'deleting ugoira {0}'.format(ugoira_file))
        os.remove(ugoira_file)

    # set last-modified and last-accessed timestamp
    if image is not None and _config.setLastModified and exportname is not None and os.path.isfile(exportname):
        ts = time.mktime(image.worksDateDateTime.timetuple())
        os.utime(exportname, (ts, ts))


def ugoira2apng(ugoira_file, exportname, delete_ugoira, image=None):
    print_and_log('info', 'processing ugoira to apng...')
    temp_folder = tempfile.mkdtemp()
    temp_name = temp_folder + os.sep + "temp.png"

    with zipfile.ZipFile(ugoira_file) as f:
        f.extractall(temp_folder)

    filenames = os.listdir(temp_folder)
    filenames.remove('animation.json')
    anim_info = json.load(open(temp_folder + '/animation.json'))

    files = []
    for info in anim_info["frames"]:
        fImage = temp_folder + os.sep + info["file"]
        delay = info["delay"]
        files.append((fImage, delay))

    im = APNG()
    for fImage, delay in files:
        im.append_file(fImage, delay=delay)
    im.save(temp_name)
    shutil.move(temp_name, exportname)
    print_and_log('info', 'ugoira exported to: ' + exportname)

    shutil.rmtree(temp_folder)
    if delete_ugoira:
        print_and_log('info', 'deleting ugoira {0}'.format(ugoira_file))
        os.remove(ugoira_file)

    # set last-modified and last-accessed timestamp
    if image is not None and _config.setLastModified and exportname is not None and os.path.isfile(exportname):
        ts = time.mktime(image.worksDateDateTime.timetuple())
        os.utime(exportname, (ts, ts))


def ugoira2webm(ugoira_file,
                exportname,
                delete_ugoira,
                ffmpeg=u"ffmpeg",
                codec="libvpx-vp9",
                param="-lossless 1 -vsync 2 -r 999 -pix_fmt yuv420p",
                extension="webm",
                image=None):
    ''' modified based on https://github.com/tsudoko/ugoira-tools/blob/master/ugoira2webm/ugoira2webm.py '''
    d = tempfile.mkdtemp(prefix="ugoira2webm")
    d = d.replace(os.sep, '/')

    try:
        frames = {}
        ffconcat = "ffconcat version 1.0\n"

        if exportname is None or len(exportname) == 0:
            name = '.'.join(ugoira_file.split('.')[:-1])
            exportname = u"{0}.{1}".format(os.path.basename(name), extension)

        tempname = d + "/temp." + extension

        with zipfile.ZipFile(ugoira_file) as f:
            f.extractall(d)

        with open(d + "/animation.json") as f:
            frames = json.load(f)['frames']

        for i in frames:
            ffconcat += "file " + i['file'] + '\n'
            ffconcat += "duration " + str(float(i['delay']) / 1000) + '\n'
        # Fix ffmpeg concat demuxer as described in issue #381
        # this will increase the frame count, but will fix the last frame timestamp issue.
        ffconcat += "file " + frames[-1]['file'] + '\n'

        with open(d + "/i.ffconcat", "w") as f:
            f.write(ffconcat)

        cmd = u"{0} -y -i \"{1}/i.ffconcat\" -c:v {2} {3} \"{4}\""
        cmd = cmd.format(ffmpeg, d, codec, param, tempname)
        ffmpeg_args = shlex.split(cmd)
        p = subprocess.Popen(ffmpeg_args, stderr=subprocess.PIPE)

        # progress report
        chatter = ""
        print_and_log('info', u"Start encoding {0}".format(exportname))
        while p.stderr:
            buff = p.stderr.readline().decode('utf-8').rstrip('\n')
            chatter += buff
            if buff.endswith("\r"):
                if chatter.find("frame=") > 0:
                    print(chatter.strip(), os.linesep, end=' ')
                chatter = ""
            if len(buff) == 0:
                break

        ret = p.wait()
        shutil.move(tempname, exportname)

        if delete_ugoira:
            print_and_log('info', 'deleting ugoira {0}'.format(ugoira_file))
            os.remove(ugoira_file)

        if ret is not None:
            print("done with status= {0}".format(ret))
        # set last-modified and last-accessed timestamp
        if image is not None and _config.setLastModified and exportname is not None and os.path.isfile(exportname):
            ts = time.mktime(image.worksDateDateTime.timetuple())
            os.utime(exportname, (ts, ts))

    finally:
        shutil.rmtree(d)


def parse_date_time(worksDate, dateFormat):
    if dateFormat is not None and len(dateFormat) > 0 and '%' in dateFormat:
        # use the user defined format
        worksDateDateTime = None
        try:
            worksDateDateTime = datetime.strptime(worksDate, dateFormat)
        except ValueError:
            get_logger().exception('Error when parsing datetime: %s using date format %s', worksDate, dateFormat)
            raise
    else:
        worksDate = worksDate.replace(u'/', u'-')
        if worksDate.find('-') > -1:
            try:
                worksDateDateTime = datetime.strptime(worksDate, u'%m-%d-%Y %H:%M')
            except ValueError:
                get_logger().exception('Error when parsing datetime: %s', worksDate)
                worksDateDateTime = datetime.strptime(worksDate.split(" ")[0], u'%Y-%m-%d')
        else:
            tempDate = worksDate.replace(u'年', '-').replace(u'月', '-').replace(u'日', '')
            worksDateDateTime = datetime.strptime(tempDate, '%Y-%m-%d %H:%M')

    return worksDateDateTime


def encode_tags(tags):
    if not tags.startswith("%"):
        try:
            # Encode the tags
            tags = tags.replace(' ', '%%space%%')
            tags = urllib.parse.quote_plus(tags).replace('%25%25space%25%25', '%20')
        except UnicodeDecodeError:
            try:
                # from command prompt
                tags = urllib.request.quote(tags.decode(sys.stdout.encoding).encode("utf8"))
            except UnicodeDecodeError:
                print_and_log('error', 'Cannot decode tags, use URL Encoder (http://meyerweb.com/eric/tools/dencoder/) and paste result.')
    return tags


def check_version():
    import PixivBrowserFactory
    br = PixivBrowserFactory.getBrowser()
    result = br.open_with_retry("https://raw.githubusercontent.com/Nandaka/PixivUtil2/master/PixivConstant.py", retry=3)
    page = result.read().decode('utf-8')
    result.close()
    latest_version_full = re.findall(r"PIXIVUTIL_VERSION = '(\d+)(.*)'", page)

    latest_version_int = int(latest_version_full[0][0])
    curr_version_int = int(re.findall(r"(\d+)", PixivConstant.PIXIVUTIL_VERSION)[0])
    is_beta = True if latest_version_full[0][1].find("beta") >= 0 else False
    if latest_version_int > curr_version_int and is_beta:
        print_and_log("info", "New beta version available: {0}".format(latest_version_full[0]))
    elif latest_version_int > curr_version_int:
        print_and_log("info", "New version available: {0}".format(latest_version_full[0]))


def decode_tags(tags):
    # decode tags.
    try:
        if tags.startswith("%"):
            search_tags = urllib.parse.unquote_plus(tags)
        else:
            search_tags = tags
    except UnicodeDecodeError:
        # From command prompt
        search_tags = tags.decode(sys.stdout.encoding).encode("utf8")
    return search_tags


# Issue 420
class LocalUTCOffsetTimezone(tzinfo):
    def __init__(self, offset=0, name=None):
        super(LocalUTCOffsetTimezone, self).__init__()
        self.offset = time.timezone * -1
        is_dst = time.localtime().tm_isdst
        self.name = time.tzname[0] if not is_dst and len(time.tzname) > 1 else time.tzname[1]

    def __str__(self):
        offset1 = abs(int(self.offset / 60 / 60))
        offset2 = abs(int(self.offset / 60 % 60))
        return "{0}{1:02d}:{2:02d}".format("-" if self.offset < 0 else "+", offset1, offset2)

    def __repr__(self):
        return self.__str__()

    def utcoffset(self, dt):
        return timedelta(seconds=self.offset)

    def tzname(self, dt):
        return self.name

    def dst(self, dt):
        return timedelta(0) if (time.localtime().tm_isdst == 0) else timedelta(seconds=time.timezone - time.altzone)

    def getTimeZoneOffset(self):
        offset = time.timezone if (time.localtime().tm_isdst == 0) else time.altzone
        return offset / 60 / 60 * -1

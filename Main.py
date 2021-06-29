from __future__ import annotations
from collections import OrderedDict
import copy
import hashlib
import io
import itertools
import logging
import os
import platform
import random
import shutil
import subprocess
import sys
import struct
import time
import zipfile
from typing import Optional


from Cosmetics import CosmeticsLog, patch_cosmetics
from EntranceShuffle import set_entrances
from Dungeon import create_dungeons
from Fill import distribute_items_restrictive, ShuffleError
from Goals import update_goal_items, maybe_set_misc_item_hints, replace_goal_names
from Hints import buildGossipHints
from HintList import clear_hint_exclusion_cache, misc_item_hint_table, misc_location_hint_table
from Item import Item
from ItemPool import generate_itempool
from MBSDIFFPatch import apply_ootr_3_web_patch
from HintList import clearHintExclusionCache, misc_item_hint_table, misc_location_hint_table
from Models import patch_model_adult, patch_model_child
from N64Patch import create_patch_file, apply_patch_file


from Patches import patch_rom
from Rom import Rom
from Rules import set_rules, set_shop_rules
from Plandomizer import Distribution
from Search import Search, RewindableSearch

from LocationList import set_drop_location_names
from Settings import Settings
from SettingsList import setting_infos, logic_tricks
from Spoiler import Spoiler
from Utils import default_output_path, is_bundled, run_process, data_path
from World import World

from version import __version__


def main(settings: Settings, max_attempts: int = 10) -> Spoiler:
    clear_hint_exclusion_cache()
    logger = logging.getLogger('')
    start = time.process_time()

    rom = resolve_settings(settings)

    max_attempts = max(max_attempts, 1)
    spoiler = None
    for attempt in range(1, max_attempts + 1):
        try:
            spoiler = generate(settings)
            break
        except ShuffleError as e:
            logger.warning('Failed attempt %d of %d: %s', attempt, max_attempts, e)
            if attempt >= max_attempts:
                raise
            else:
                logger.info('Retrying...\n\n')
            settings.reset_distribution()
    if spoiler is None:
        raise RuntimeError("Generation failed.")
    patch_and_output(settings, spoiler, rom)
    logger.debug('Total Time: %s', time.process_time() - start)
    return spoiler


def resolve_settings(settings: Settings) -> Optional[Rom]:
    logger = logging.getLogger('')

    old_tricks = settings.allowed_tricks
    settings.load_distribution()

    # compare pointers to lists rather than contents, so even if the two are identical
    # we'll still log the error and note the dist file overrides completely.
    if old_tricks and old_tricks is not settings.allowed_tricks:
        logger.error('Tricks are set in two places! Using only the tricks from the distribution file.')

    for trick in logic_tricks.values():
        settings.settings_dict[trick['name']] = trick['name'] in settings.allowed_tricks

    # we load the rom before creating the seed so that errors get caught early
    outputting_specific_world = settings.create_uncompressed_rom or settings.create_compressed_rom or settings.create_wad_file
    using_rom = outputting_specific_world or settings.create_patch_file or settings.patch_without_output
    if not (using_rom or settings.patch_without_output) and not settings.create_spoiler:
        raise Exception('You must have at least one output type or spoiler log enabled to produce anything.')

    if using_rom:
        rom = Rom(settings.rom)
    else:
        rom = None

    if not settings.world_count:
        settings.world_count = 1
    elif settings.world_count < 1 or settings.world_count > 255:
        raise Exception('World Count must be between 1 and 255')

    # Bounds-check the player_num settings, in case something's gone wrong we want to know.
    if settings.player_num < 1:
        raise Exception(f'Invalid player num: {settings.player_num}; must be between (1, {settings.world_count})')
    if settings.player_num > settings.world_count:
        if outputting_specific_world:
            raise Exception(f'Player Num is {settings.player_num}; must be between (1, {settings.world_count})')
        settings.player_num = settings.world_count

    # Set to a custom hint distribution if plando is overriding the distro
    if len(settings.hint_dist_user) != 0:
        settings.hint_dist = 'custom'

    logger.info('OoT Randomizer Version %s  -  Seed: %s', __version__, settings.seed)
    settings.remove_disabled()
    logger.info('(Original) Settings string: %s\n', settings.settings_string)
    random.seed(settings.numeric_seed)
    settings.resolve_random_settings(cosmetic=False)
    logger.debug(settings.get_settings_display())
    return rom


def generate(settings: Settings) -> Spoiler:
    worlds = build_world_graphs(settings)
    place_items(worlds)
    for world in worlds:
        world.distribution.configure_effective_starting_items(worlds, world)
    if worlds[0].enable_goal_hints:
        replace_goal_names(worlds)
    return make_spoiler(settings, worlds)


def build_world_graphs(settings: Settings) -> list[World]:
    logger = logging.getLogger('')
    worlds = []
    for i in range(0, settings.world_count):
        worlds.append(World(i, settings.copy()))

    savewarps_to_connect = []
    for id, world in enumerate(worlds):
        logger.info('Generating World %d.' % (id + 1))
        logger.info('Creating Overworld')

        # Load common json rule files (those used regardless of MQ status)
        if settings.logic_rules == 'glitched':
            path = 'Glitched World'
        else:
            path = 'World'
        path = data_path(path)

        for filename in ('Overworld.json', 'Bosses.json'):
            savewarps_to_connect += world.load_regions_from_json(os.path.join(path, filename))

        # Compile the json rules based on settings
        savewarps_to_connect += world.create_dungeons()
        world.create_internal_locations()

        if settings.shopsanity != 'off':
            world.random_shop_prices()
        world.set_scrub_prices()

        logger.info('Calculating Access Rules.')
        set_rules(world)

        logger.info('Generating Item Pool.')
        generate_itempool(world)
        set_shop_rules(world)
        world.set_drop_location_names()
        world.fill_bosses()

    if settings.triforce_hunt:
        settings.distribution.configure_triforce_hunt(worlds)

    logger.info('Setting Entrances.')
    set_entrances(worlds, savewarps_to_connect)
    return worlds


def place_items(worlds: list[World]) -> None:
    logger = logging.getLogger('')
    logger.info('Fill the world.')
    distribute_items_restrictive(worlds)


def make_spoiler(settings: Settings, worlds: list[World]) -> Spoiler:
    logger = logging.getLogger('')
    spoiler = Spoiler(worlds)
    if settings.create_spoiler:
        logger.info('Calculating playthrough.')
        spoiler.create_playthrough()
        #window.update_progress(45)
    if settings.create_spoiler or settings.hints != 'none':
        window.update_status('Calculating Hint Data')
        logger.info('Calculating coarse spheres.')
        compute_coarse_spheres(spoiler)
        window.update_progress(50)
        logger.info('Calculating hint data.')
        update_goal_items(spoiler)
        build_gossip_hints(spoiler, worlds)
    elif any(world.dungeon_rewards_hinted for world in worlds) or any(hint_type in settings.misc_hints for hint_type in misc_item_hint_table) or any(hint_type in settings.misc_hints for hint_type in misc_location_hint_table):
        spoiler.find_misc_hint_items()
    spoiler.build_file_hash()
    return spoiler


def prepare_rom(spoiler: Spoiler, world: World, rom: Rom, settings: Settings, rng_state: Optional[tuple] = None, restore: bool = True) -> CosmeticsLog:
    if rng_state:
        random.setstate(rng_state)
        # Use different seeds for each world when patching.
        seed = int(random.getrandbits(256))
        for i in range(0, world.id):
            seed = int(random.getrandbits(256))
        random.seed(seed)

    if restore:
        rom.restore()
    patch_rom(spoiler, world, rom)
    cosmetics_log = patch_cosmetics(settings, rom)
    if not settings.generating_patch_file:
        if settings.model_adult != "Default" or len(settings.model_adult_filepicker) > 0:
            patch_model_adult(rom, settings, cosmetics_log)
        if settings.model_child != "Default" or len(settings.model_child_filepicker) > 0:
            patch_model_child(rom, settings, cosmetics_log)
    rom.update_header()
    return cosmetics_log


def compress_rom(input_file: str, output_file: str, delete_input: bool = False) -> None:
    logger = logging.getLogger('')
    compressor_path = "./" if is_bundled() else "bin/Compress/"
    if platform.system() == 'Windows':
        if platform.machine() == 'AMD64':
            compressor_path += "Compress.exe"
        elif platform.machine() == 'ARM64':
            compressor_path += "Compress_ARM64.exe"
        else:
            compressor_path += "Compress32.exe"
    elif platform.system() == 'Linux':
        if platform.machine() in ('arm64', 'aarch64', 'aarch64_be', 'armv8b', 'armv8l'):
            compressor_path += "Compress_ARM64"
        elif platform.machine() in ('arm', 'armv7l', 'armhf'):
            compressor_path += "Compress_ARM32"
        else:
            compressor_path += "Compress"
    elif platform.system() == 'Darwin':
        if platform.machine() == 'arm64':
            compressor_path += "Compress_ARM64.out"
        else:
            compressor_path += "Compress.out"
    else:
        logger.info("OS not supported for ROM compression.")
        raise Exception("This operating system does not support ROM compression. You may only output patch files or uncompressed ROMs.")

    run_process(logger, [compressor_path, input_file, output_file])
    if delete_input:
        os.remove(input_file)


def generate_wad(wad_file: str, rom_file: str, output_file: str, channel_title: str, channel_id: str, delete_input: bool = False) -> None:
    logger = logging.getLogger('')
    if wad_file == "" or wad_file is None:
        raise Exception("Unspecified base WAD file.")
    if not os.path.isfile(wad_file):
        raise Exception("Cannot open base WAD file.")

    gzinject_path = "./" if is_bundled() else "bin/gzinject/"
    gzinject_patch_path = gzinject_path + "ootr.gzi"
    if platform.system() == 'Windows':
        if platform.machine() == 'AMD64':
            gzinject_path += "gzinject.exe"
        elif platform.machine() == 'ARM64':
            gzinject_path += "gzinject_ARM64.exe"
        else:
            gzinject_path += "gzinject32.exe"
    elif platform.system() == 'Linux':
        if platform.machine() in ('arm64', 'aarch64', 'aarch64_be', 'armv8b', 'armv8l'):
            gzinject_path += "gzinject_ARM64"
        elif platform.machine() in ('arm', 'armv7l', 'armhf'):
            gzinject_path += "gzinject_ARM32"
        else:
            gzinject_path += "gzinject"
    elif platform.system() == 'Darwin':
        if platform.machine() == 'arm64':
            gzinject_path += "gzinject_ARM64.out"
        else:
            gzinject_path += "gzinject.out"
    else:
        logger.info("OS not supported for WAD generation.")
        raise Exception("This operating system does not support outputting .wad files.")

    run_process(logger, [gzinject_path, "-a", "genkey"], b'45e')
    run_process(logger, [gzinject_path, "-a", "inject", "--rom", rom_file, "--wad", wad_file,
                         "-o", output_file, "-i", channel_id, "-t", channel_title,
                         "-p", gzinject_patch_path, "--cleanup"])
    os.remove("common-key.bin")
    if delete_input:
        os.remove(rom_file)


def patch_and_output(settings: Settings, spoiler: Spoiler, rom: Optional[Rom]) -> None:
    logger = logging.getLogger('')
    worlds = spoiler.worlds
    cosmetics_log = None

    settings_string_hash = hashlib.sha1(settings.settings_string.encode('utf-8')).hexdigest().upper()[:5]
    if settings.output_file:
        output_filename_base = settings.output_file
    else:
        output_filename_base = f"OoT_{settings_string_hash}_{settings.seed}"
        if settings.world_count > 1:
            output_filename_base += f"_W{settings.world_count}"

    output_dir = default_output_path(settings.output_dir)

    compressed_rom = settings.create_compressed_rom or settings.create_wad_file
    uncompressed_rom = compressed_rom or settings.create_uncompressed_rom
    generate_rom = uncompressed_rom or settings.create_patch_file or settings.patch_without_output
    separate_cosmetics = settings.create_patch_file and uncompressed_rom

    if generate_rom and rom is not None:
        rng_state = random.getstate()
        file_list = []
        restore_rom = False
        for world in worlds:
            # If we aren't creating a patch file and this world isn't the one being outputted, move to the next world.
            if not (settings.create_patch_file or world.id == settings.player_num - 1):
                continue

            if settings.world_count > 1:
                logger.info(f"Patching ROM: Player {world.id + 1}")
                player_filename_suffix = f"P{world.id + 1}"
            else:
                logger.info('Patching ROM')
                player_filename_suffix = ""

            settings.generating_patch_file = settings.create_patch_file
            patch_cosmetics_log = prepare_rom(spoiler, world, rom, settings, rng_state, restore_rom)
            restore_rom = True

            if settings.create_patch_file:
                patch_filename = f"{output_filename_base}{player_filename_suffix}.zpf"
                logger.info(f"Creating Patch File: {patch_filename}")
                output_path = os.path.join(output_dir, patch_filename)
                file_list.append(patch_filename)
                create_patch_file(rom, output_path)

                # Cosmetics Log for patch file only.
                if settings.create_cosmetics_log and patch_cosmetics_log:
                    if separate_cosmetics:
                        cosmetics_log_filename = f"{output_filename_base}{player_filename_suffix}.zpf_Cosmetics.json"
                    else:
                        cosmetics_log_filename = f"{output_filename_base}{player_filename_suffix}_Cosmetics.json"
                    logger.info(f"Creating Cosmetics Log: {cosmetics_log_filename}")
                    patch_cosmetics_log.to_file(os.path.join(output_dir, cosmetics_log_filename))
                    file_list.append(cosmetics_log_filename)

            # If we aren't outputting an uncompressed ROM, move to the next world.
            if not uncompressed_rom or world.id != settings.player_num - 1:
                continue

            uncompressed_filename = f"{output_filename_base}{player_filename_suffix}_uncompressed.z64"
            uncompressed_path = os.path.join(output_dir, uncompressed_filename)
            logger.info(f"Saving Uncompressed ROM: {uncompressed_filename}")
            if separate_cosmetics:
                settings.generating_patch_file = False
                cosmetics_log = prepare_rom(spoiler, world, rom, settings, rng_state, restore_rom)
            else:
                cosmetics_log = patch_cosmetics_log
            rom.write_to_file(uncompressed_path)
            logger.info("Created uncompressed ROM at: %s" % uncompressed_path)

            # If we aren't compressing the ROM, we're done with this world.
            if not compressed_rom:
                continue

            compressed_filename = f"{output_filename_base}{player_filename_suffix}.z64"
            compressed_path = os.path.join(output_dir, compressed_filename)
            logger.info(f"Compressing ROM: {compressed_filename}")
            compress_rom(uncompressed_path, compressed_path, not settings.create_uncompressed_rom)
            logger.info("Created compressed ROM at: %s" % compressed_path)

            # If we aren't generating a WAD, we're done with this world.
            if not settings.create_wad_file:
                continue

            wad_filename = f"{output_filename_base}{player_filename_suffix}.wad"
            wad_path = os.path.join(output_dir, wad_filename)
            logger.info(f"Generating WAD file: {wad_filename}")
            channel_title = settings.wad_channel_title if settings.wad_channel_title != "" and settings.wad_channel_title is not None else "OoTRandomizer"
            channel_id = settings.wad_channel_id if settings.wad_channel_id != "" and settings.wad_channel_id is not None else "OOTE"
            generate_wad(settings.wad_file, compressed_path, wad_path, channel_title, channel_id, not settings.create_compressed_rom)
            logger.info("Created WAD file at: %s" % wad_path)

        # World loop over, make the patch archive if applicable.
        if settings.world_count > 1 and settings.create_patch_file:
            patch_archive_filename = f"{output_filename_base}.zpfz"
            patch_archive_path = os.path.join(output_dir, patch_archive_filename)
            logger.info(f"Creating Patch Archive: {patch_archive_filename}")
            with zipfile.ZipFile(patch_archive_path, mode="w") as patch_archive:
                for file in file_list:
                    file_path = os.path.join(output_dir, file)
                    patch_archive.write(file_path, file.replace(output_filename_base, '').replace('.zpf_Cosmetics', '_Cosmetics'), compress_type=zipfile.ZIP_DEFLATED)
            for file in file_list:
                os.remove(os.path.join(output_dir, file))
            logger.info("Created patch file archive at: %s" % patch_archive_path)

    if not settings.create_spoiler or settings.output_settings:
        settings.distribution.update_spoiler(spoiler, False)
        settings_path = os.path.join(output_dir, '%s_Settings.json' % output_filename_base)
        settings.distribution.to_file(settings_path, False)
        logger.info("Created settings log at: %s" % ('%s_Settings.json' % output_filename_base))
    if settings.create_spoiler:
        settings.distribution.update_spoiler(spoiler, True)
        spoiler_path = os.path.join(output_dir, '%s_Spoiler.json' % output_filename_base)
        settings.distribution.to_file(spoiler_path, True)
        logger.info("Created spoiler log at: %s" % ('%s_Spoiler.json' % output_filename_base))

    if settings.create_cosmetics_log and cosmetics_log:
        if settings.world_count > 1 and not settings.output_file:
            filename = "%sP%d_Cosmetics.json" % (output_filename_base, settings.player_num)
        else:
            filename = '%s_Cosmetics.json' % output_filename_base
        cosmetic_path = os.path.join(output_dir, filename)
        cosmetics_log.to_file(cosmetic_path)
        logger.info("Created cosmetic log at: %s" % cosmetic_path)

    if settings.enable_distribution_file:
        try:
            filename = os.path.join(output_dir, '%s_Distribution.json' % output_filename_base)
            shutil.copyfile(settings.distribution_file, filename)
            logger.info("Copied distribution file to: %s" % filename)
        except:
            logger.info('Distribution file copy failed.')

    if cosmetics_log and cosmetics_log.errors:
        logger.info('Success: Rom patched successfully. Some cosmetics could not be applied.')
    else:
        logger.info('Success: Rom patched successfully')


def from_patch_file(settings: Settings) -> None:
    start = time.process_time()
    logger = logging.getLogger('')

    compressed_rom = settings.create_compressed_rom or settings.create_wad_file
    uncompressed_rom = compressed_rom or settings.create_uncompressed_rom

    # we load the rom before creating the seed so that error get caught early
    if not uncompressed_rom:
        raise Exception('You must select a valid Output Type when patching from a patch file.')

    rom = Rom(settings.rom)
    logger.info('Patching ROM.')

    filename_split = os.path.basename(settings.patch_file).rpartition('.')

    if settings.output_file:
        output_filename_base = settings.output_file
    else:
        output_filename_base = filename_split[0]

    extension = filename_split[-1]

    output_dir = default_output_path(settings.output_dir)
    output_path = os.path.join(output_dir, output_filename_base)

    logger.info('Patching ROM')
    if extension == 'patch':
        apply_ootr_3_web_patch(settings, rom)
        create_patch_file(rom, output_path + '.zpf')
    else:
        subfile = None
        if extension == 'zpfz':
            subfile = f"P{settings.player_num}.zpf"
            if not settings.output_file:
                output_path += f"P{settings.player_num}"
        apply_patch_file(rom, settings, subfile)
    cosmetics_log = None
    if settings.repatch_cosmetics:
        cosmetics_log = patch_cosmetics(settings, rom)
        if settings.model_adult != "Default" or len(settings.model_adult_filepicker) > 0:
            patch_model_adult(rom, settings, cosmetics_log)
        if settings.model_child != "Default" or len(settings.model_child_filepicker) > 0:
            patch_model_child(rom, settings, cosmetics_log)

    logger.info('Saving Uncompressed ROM')
    uncompressed_path = output_path + '_uncompressed.z64'
    rom.write_to_file(uncompressed_path)
    logger.info("Created uncompressed rom at: %s" % uncompressed_path)

    if compressed_rom:
        logger.info('Compressing ROM')
        compressed_path = output_path + '.z64'
        compress_rom(uncompressed_path, compressed_path, not settings.create_uncompressed_rom)
        logger.info("Created compressed rom at: %s" % compressed_path)

        if settings.create_wad_file:
            wad_path = output_path + '.wad'
            channel_title = settings.wad_channel_title if settings.wad_channel_title != "" and settings.wad_channel_title is not None else "OoTRandomizer"
            channel_id = settings.wad_channel_id if settings.wad_channel_id != "" and settings.wad_channel_id is not None else "OOTE"
            generate_wad(settings.wad_file, compressed_path, wad_path, channel_title, channel_id,
                         not settings.create_compressed_rom)
            logger.info("Created WAD file at: %s" % wad_path)

    if settings.create_cosmetics_log and cosmetics_log:
        if settings.world_count > 1 and not settings.output_file:
            filename = "%sP%d_Cosmetics.json" % (output_filename_base, settings.player_num)
        else:
            filename = '%s_Cosmetics.json' % output_filename_base
        cosmetic_path = os.path.join(output_dir, filename)
        cosmetics_log.to_file(cosmetic_path)
        logger.info("Created cosmetic log at: %s" % cosmetic_path)

    if cosmetics_log and cosmetics_log.errors:
        logger.info('Success: Rom patched successfully. Some cosmetics could not be applied.')
    else:
        logger.info('Success: Rom patched successfully')

    logger.debug('Total Time: %s', time.process_time() - start)


def cosmetic_patch(settings: Settings) -> None:
    start = time.process_time()
    logger = logging.getLogger('')

    if settings.patch_file == '':
        raise Exception('Cosmetic Only must have a patch file supplied.')

    rom = Rom(settings.rom)

    logger.info('Patching ROM.')

    filename_split = os.path.basename(settings.patch_file).rpartition('.')

    if settings.output_file:
        outfilebase = settings.output_file
    else:
        outfilebase = filename_split[0]

    extension = filename_split[-1]

    output_dir = default_output_path(settings.output_dir)
    output_path = os.path.join(output_dir, outfilebase)

    logger.info('Patching ROM')
    if extension == 'zpf':
        subfile = None
    else:
        subfile = 'P%d.zpf' % (settings.player_num)
    apply_patch_file(rom, settings, subfile)

    # clear changes from the base patch file
    patched_base_rom = copy.copy(rom.buffer)
    rom.changed_address = {}
    rom.changed_dma = {}
    rom.force_patch = []

    patchfilename = '%s_Cosmetic.zpf' % output_path
    cosmetics_log = patch_cosmetics(settings, rom)

    # base the new patch file on the base patch file
    rom.original.buffer = patched_base_rom
    rom.update_header()
    create_patch_file(rom, patchfilename)
    logger.info("Created patchfile at: %s" % patchfilename)

    if settings.create_cosmetics_log and cosmetics_log:
        if settings.world_count > 1 and not settings.output_file:
            filename = "%sP%d_Cosmetics.json" % (outfilebase, settings.player_num)
        else:
            filename = '%s_Cosmetics.json' % outfilebase
        cosmetic_path = os.path.join(output_dir, filename)
        cosmetics_log.to_file(cosmetic_path)
        logger.info("Created cosmetic log at: %s" % cosmetic_path)

    if cosmetics_log and cosmetics_log.errors:
        logger.info('Success: Rom patched successfully. Some cosmetics could not be applied.')
    else:
        logger.info('Success: Rom patched successfully')

    logger.debug('Total Time: %s', time.process_time() - start)


def diff_roms(settings: Settings, diff_rom_file: str) -> None:
    start = time.process_time()
    logger = logging.getLogger('')

    logger.info('Loading base ROM.')
    rom = Rom(settings.rom)
    rom.force_patch = []

    filename_split = os.path.basename(diff_rom_file).rpartition('.')
    output_filename_base = settings.output_file if settings.output_file else filename_split[0]
    output_dir = default_output_path(settings.output_dir)
    output_path = os.path.join(output_dir, output_filename_base)

    logger.info('Loading patched ROM.')
    rom.read_rom(diff_rom_file, f"{output_path}_decomp.z64", verify_crc=False)
    try:
        os.remove(f"{output_path}_decomp.z64")
    except FileNotFoundError:
        pass

    logger.info('Searching for changes.')
    rom.rescan_changed_bytes()
    rom.scan_dmadata_update(assume_move=True)

    logger.info('Creating patch file.')
    create_patch_file(rom, f"{output_path}.zpf")
    logger.info(f"Created patchfile at: {output_path}.zpf")
    logger.info('Done. Enjoy.')
    logger.debug('Total Time: %s', time.process_time() - start)


def copy_worlds(worlds):
    worlds = [world.copy() for world in worlds]
    Item.fix_worlds_after_copy(worlds)
    return worlds


def find_misc_hint_items(spoiler):
    search = Search([world.state for world in spoiler.worlds])
    all_locations = [location for world in spoiler.worlds for location in world.get_filled_locations()]
    for location in search.iter_reachable_locations(all_locations[:]):
        search.collect(location.item)
        # include locations that are reachable but not part of the spoiler log playthrough in misc. item hints
        maybe_set_misc_item_hints(location)
        all_locations.remove(location)
    for location in all_locations:
        # finally, collect unreachable locations for misc. item hints
        maybe_set_misc_item_hints(location)


def compute_coarse_spheres(spoiler):
    worlds = spoiler.worlds
    worlds = copy_worlds(worlds)
    collection_spheres = []
    
    # this function tracks spheres in a simplified way: it increments spheres only when "noteworthy" items are collected
    # "noteworthy" items are defined as follows
    def is_noteworthy(item):
        if item.type == "Song":
            return True
        if item.type == "Item" and item.advancement:
            return item.name not in ["Deliver Letter", "Gerudo Membership Card", "Magic Bean"]
        return False
     
    # to keep a readable and useful spoiler log, we only log certain items:
    def must_be_logged(item, noteworthy):
        nonlocal collection_spheres, item_locations, spoiler_locations
        if noteworthy:
            if item.location in spoiler_locations:
                return item.location not in collection_spheres[0]
        else:
            if item.type == "DungeonReward":
                return item.location in spoiler_locations
        return False

    # get list of all of the progressive items that can appear in hints
    # all_locations: all progressive items. have to collect from these
    # item_locations: only the ones that should appear as "required"/WotH
    all_locations = [location for world in worlds for location in world.get_filled_locations()]
    item_locations = {location for location in all_locations if location.item.majoritem and not location.locked and location.item.name != 'Triforce Piece'}
    
    # if the playthrough was generated, filter the list of locations to the
    # locations in the playthrough. The required locations is a subset of these
    # locations. Can't use the locations directly since they are location to the
    # copied spoiler world, so must compare via name and world id
    if spoiler.playthrough:
        translate = lambda loc: worlds[loc.world.id].get_location(loc.name)
        spoiler_locations = set(map(translate, itertools.chain.from_iterable(spoiler.playthrough.values())))
        item_locations &= spoiler_locations
    else:
        spoiler_locations = all_locations
    
    search = Search([world.state for world in worlds])

    # Create "-1" sphere
    sphere_number = -1
    collection_spheres.append({})
    
    distribution = spoiler.settings.distribution.world_dists[0]
    for (name, record) in distribution.starting_items.items():
        item = Item(name, world=worlds[0])
        search.state_list[0].collect(item)

    item = worlds[0].get_location("Links Pocket").item
    search.state_list[0].collect(item)
    collection_spheres[-1][item.location] = item.name

    if spoiler.settings.skip_child_zelda:
        location = worlds[0].get_location("HC Zeldas Letter")
        item = location.item
        search.state_list[0].collect(item)
        collection_spheres[-1][item.location] = item.name
        location = worlds[0].get_location("Song from Impa")
        item = location.item
        search.state_list[0].collect(item)
        collection_spheres[-1][item.location] = item.name
    
    # Compute next spheres
    had_reachable_locations = True
    items_to_delay = []
    items_to_collect = []
    increment_sphere = True
    while had_reachable_locations:
        child_regions, adult_regions, visited_locations = search.next_sphere()
        
        if increment_sphere:
            sphere_number += 1
            collection_spheres.append({})
            had_reachable_locations = False

        location = None
        for loc in all_locations:
            if loc in visited_locations:
                continue
            # Check adult first; it's the most likely.
            if (loc.parent_region in adult_regions
                    and loc.access_rule(search.state_list[loc.world.id], spot=loc, age='adult')):
                had_reachable_locations = True
                # Mark it visited for this algorithm
                visited_locations.add(loc)
                location = loc

            elif (loc.parent_region in child_regions
                  and loc.access_rule(search.state_list[loc.world.id], spot=loc, age='child')):
                had_reachable_locations = True
                # Mark it visited for this algorithm
                visited_locations.add(loc)
                location = loc
            
            # If location is reachable, add its item to the right list
            if location:
                item = location.item
                if location in collection_spheres[0]:
                    # If the location was already in sphere -1, ignore it
                    pass
                elif is_noteworthy(item):
                    items_to_delay.append(location.item)
                else:
                    items_to_collect.append(location.item)
                location = None
        
        # If some non-remarkable items have been found, collect them and don't open a new sphere
        if len(items_to_collect) > 0:
            for item in items_to_collect:
                search.state_list[item.world.id].collect(item)
                if must_be_logged(item, False):
                    collection_spheres[-1][item.location] = item.name
            items_to_collect = []
            increment_sphere = False
            had_reachable_locations = True
        # Otherwise collect everything remarkable met in the current sphere and open a new one
        else:
            for item in items_to_delay:
                search.state_list[item.world.id].collect(item)
                if must_be_logged(item, True):
                    collection_spheres[-1][item.location] = item.name
            items_to_delay = []
            increment_sphere = True
    
    # Remove possibly empty spheres at the tail
    while len(collection_spheres) > 0 and len(collection_spheres[-1]) == 0:
        del collection_spheres[-1]
    spoiler.coarse_spheres = OrderedDict((str(i-1), {location: location.item for location in sphere}) for i, sphere in enumerate(collection_spheres))
    
def update_required_items(spoiler):
    worlds = spoiler.worlds

    # get list of all of the progressive items that can appear in hints
    # all_locations: all progressive items. have to collect from these
    # item_locations: only the ones that should appear as "required"/WotH
    all_locations = [location for world in worlds for location in world.get_filled_locations()]
    # Set to test inclusion against
    item_locations = {location for location in all_locations if location.item.majoritem and not location.locked and location.item.name != 'Triforce Piece'}

    # if the playthrough was generated, filter the list of locations to the
    # locations in the playthrough. The required locations is a subset of these
    # locations. Can't use the locations directly since they are location to the
    # copied spoiler world, so must compare via name and world id
    if spoiler.playthrough:
        translate = lambda loc: worlds[loc.world.id].get_location(loc.name)
        spoiler_locations = set(map(translate, itertools.chain.from_iterable(spoiler.playthrough.values())))
        item_locations &= spoiler_locations
        # Skip even the checks
        _maybe_set_light_arrows = lambda _: None
    else:
        _maybe_set_light_arrows = maybe_set_light_arrows

    required_locations = []

    search = Search([world.state for world in worlds])

    for location in search.iter_reachable_locations(all_locations):
        # Try to remove items one at a time and see if the game is still beatable
        if location in item_locations:
            old_item = location.item
            location.item = None
            # copies state! This is very important as we're in the middle of a search
            # already, but beneficially, has search it can start from
            if not search.can_beat_game():
                required_locations.append(location)
            location.item = old_item
            _maybe_set_light_arrows(location)
        search.state_list[location.item.world.id].collect(location.item)

    # Filter the required location to only include location in the world
    required_locations_dict = {}
    for world in worlds:
        required_locations_dict[world.id] = list(filter(lambda location: location.world.id == world.id, required_locations))
    spoiler.required_locations = required_locations_dict



def create_playthrough(spoiler):
    logger = logging.getLogger('')
    worlds = spoiler.worlds
    if worlds[0].check_beatable_only and not Search([world.state for world in worlds]).can_beat_game():
        raise RuntimeError('Game unbeatable after placing all items.')
    # create a copy as we will modify it
    old_worlds = worlds
    worlds = copy_worlds(worlds)

    # if we only check for beatable, we can do this sanity check first before writing down spheres
    if worlds[0].check_beatable_only and not Search([world.state for world in worlds]).can_beat_game():
        raise RuntimeError('Uncopied world beatable but copied world is not.')

    search = RewindableSearch([world.state for world in worlds])
    logger.debug('Initial search: %s', search.state_list[0].get_prog_items())
    # Get all item locations in the worlds
    item_locations = search.progression_locations()
    # Omit certain items from the playthrough
    internal_locations = {location for location in item_locations if location.internal}
    # Generate a list of spheres by iterating over reachable locations without collecting as we go.
    # Collecting every item in one sphere means that every item
    # in the next sphere is collectable. Will contain every reachable item this way.
    logger.debug('Building up collection spheres.')
    collection_spheres = []
    entrance_spheres = []
    remaining_entrances = set(entrance for world in worlds for entrance in world.get_shuffled_entrances())

    search.checkpoint()
    search.collect_pseudo_starting_items()
    logger.debug('With pseudo starting items: %s', search.state_list[0].get_prog_items())

    while True:
        search.checkpoint()
        # Not collecting while the generator runs means we only get one sphere at a time
        # Otherwise, an item we collect could influence later item collection in the same sphere
        collected = list(search.iter_reachable_locations(item_locations))
        if not collected: break
        random.shuffle(collected)
        # Gather the new entrances before collecting items.
        collection_spheres.append(collected)
        accessed_entrances = set(filter(search.spot_access, remaining_entrances))
        entrance_spheres.append(list(accessed_entrances))
        remaining_entrances -= accessed_entrances
        for location in collected:
            # Collect the item for the state world it is for
            search.state_list[location.item.world.id].collect(location.item)
            maybe_set_misc_item_hints(location)
    logger.info('Collected %d spheres', len(collection_spheres))
    spoiler.full_playthrough = dict((location.name, i + 1) for i, sphere in enumerate(collection_spheres) for location in sphere)
    spoiler.max_sphere = len(collection_spheres)

    # Reduce each sphere in reverse order, by checking if the game is beatable
    # when we remove the item. We do this to make sure that progressive items
    # like bow and slingshot appear as early as possible rather than as late as possible.
    required_locations = []
    for sphere in reversed(collection_spheres):
        random.shuffle(sphere)
        for location in sphere:
            # we remove the item at location and check if the game is still beatable in case the item could be required
            old_item = location.item

            # Uncollect the item and location.
            search.state_list[old_item.world.id].remove(old_item)
            search.unvisit(location)

            # Generic events might show up or not, as usual, but since we don't
            # show them in the final output, might as well skip over them. We'll
            # still need them in the final pass, so make sure to include them.
            if location.internal:
                required_locations.append(location)
                continue

            location.item = None

            # An item can only be required if it isn't already obtained or if it's progressive
            if search.state_list[old_item.world.id].item_count(old_item.solver_id) < old_item.world.max_progressions[old_item.name]:
                # Test whether the game is still beatable from here.
                logger.debug('Checking if %s is required to beat the game.', old_item.name)
                if not search.can_beat_game():
                    # still required, so reset the item
                    location.item = old_item
                    required_locations.append(location)

    # Reduce each entrance sphere in reverse order, by checking if the game is beatable when we disconnect the entrance.
    required_entrances = []
    for sphere in reversed(entrance_spheres):
        random.shuffle(sphere)
        for entrance in sphere:
            # we disconnect the entrance and check if the game is still beatable
            old_connected_region = entrance.disconnect()

            # we use a new search to ensure the disconnected entrance is no longer used
            sub_search = Search([world.state for world in worlds])

            # Test whether the game is still beatable from here.
            logger.debug('Checking if reaching %s, through %s, is required to beat the game.', old_connected_region.name, entrance.name)
            if not sub_search.can_beat_game():
                # still required, so reconnect the entrance
                entrance.connect(old_connected_region)
                required_entrances.append(entrance)

    # Regenerate the spheres as we might not reach places the same way anymore.
    search.reset() # search state has no items, okay to reuse sphere 0 cache
    collection_spheres = []
    collection_spheres.append(list(filter(lambda loc: loc.item.advancement and loc.item.world.max_progressions[loc.item.name] > 0, search.iter_pseudo_starting_locations())))
    entrance_spheres = []
    remaining_entrances = set(required_entrances)
    collected = set()
    while True:
        # Not collecting while the generator runs means we only get one sphere at a time
        # Otherwise, an item we collect could influence later item collection in the same sphere
        collected.update(search.iter_reachable_locations(required_locations))
        if not collected: break
        internal = collected & internal_locations
        if internal:
            # collect only the internal events but don't record them in a sphere
            for location in internal:
                search.state_list[location.item.world.id].collect(location.item)
            # Remaining locations need to be saved to be collected later
            collected -= internal
            continue
        # Gather the new entrances before collecting items.
        collection_spheres.append(list(collected))
        accessed_entrances = set(filter(search.spot_access, remaining_entrances))
        entrance_spheres.append(accessed_entrances)
        remaining_entrances -= accessed_entrances
        for location in collected:
            # Collect the item for the state world it is for
            search.state_list[location.item.world.id].collect(location.item)
        collected.clear()
    logger.info('Collected %d final spheres', len(collection_spheres))

    if not search.can_beat_game(False):
        logger.error('Playthrough could not beat the game!')
        # Add temporary debugging info or breakpoint here if this happens

    # Then we can finally output our playthrough
    spoiler.playthrough = OrderedDict((str(i), {location: location.item for location in sphere}) for i, sphere in enumerate(collection_spheres))
    # Copy our misc. hint items, since we set them in the world copy
    for w, sw in zip(worlds, spoiler.worlds):
        # But the actual location saved here may be in a different world
        for item_name, item_location in w.hinted_dungeon_reward_locations.items():
            sw.hinted_dungeon_reward_locations[item_name] = spoiler.worlds[item_location.world.id].get_location(item_location.name)
        for hint_type, item_location in w.misc_hint_item_locations.items():
            sw.misc_hint_item_locations[hint_type] = spoiler.worlds[item_location.world.id].get_location(item_location.name)

    if worlds[0].entrance_shuffle:
        spoiler.entrance_playthrough = OrderedDict((str(i + 1), list(sphere)) for i, sphere in enumerate(entrance_spheres))
>>>>>>> df604360 (Implements coarse spheres + last woth)

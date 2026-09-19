/*
 * restorebiome.js
 *
 * Restore Create: Nuclear irradiated biome damage.
 *
 * Minecraft / Forge:
 *   1.20.1
 *
 * WorldEdit:
 *   7.2.15
 *
 * Operation:
 *
 *   if current biome == createnuclear:irradiated_land
 *       restore to the biome produced by world generation
 *   else
 *       continue
 *
 * This script:
 *
 *   - DOES NOT regenerate terrain
 *   - DOES NOT modify blocks
 *   - DOES NOT modify entities
 *   - DOES NOT call //regen
 *   - DOES NOT alter non-irradiated biomes
 *
 * Usage:
 *
 *   /cs restorebiome dry-run
 *   /cs restorebiome
 */

var IRRADIATED_BIOME =
    "createnuclear:irradiated_land";

var world =
    player.getWorld();

var level =
    world.getWorld();

var localSession =
    context.getSession();

var selectionWorld =
    localSession.getSelectionWorld();

if (selectionWorld == null) {
    throw "No WorldEdit selection exists.";
}

var selection =
    localSession.getSelection(selectionWorld);

var minimum =
    selection.getMinimumPoint();

var maximum =
    selection.getMaximumPoint();

var dryRun =
    false;

if (argv.length > 1) {
    if (String(argv[1]).toLowerCase() == "dry-run") {
        dryRun = true;
    } else {
        throw "Unknown argument: " +
            argv[1] +
            ". Use: dry-run";
    }
}

/*
 * Minecraft stores biome information at 4x4x4 resolution.
 */
var minQX =
    Math.floor(minimum.getBlockX() / 4);

var maxQX =
    Math.floor(maximum.getBlockX() / 4);

var minQY =
    Math.floor(minimum.getBlockY() / 4);

var maxQY =
    Math.floor(maximum.getBlockY() / 4);

var minQZ =
    Math.floor(minimum.getBlockZ() / 4);

var maxQZ =
    Math.floor(maximum.getBlockZ() / 4);

var examined =
    0;

var irradiated =
    0;

var restored =
    0;

var skipped =
    0;

var failures =
    0;

var changedChunks =
    {};

player.print(
    "Biome restoration " +
    (dryRun ? "DRY RUN" : "STARTED") +
    "."
);

player.print(
    "Target: " +
    IRRADIATED_BIOME
);

player.print(
    "Selection: " +
    minimum.getBlockX() +
    ", " +
    minimum.getBlockY() +
    ", " +
    minimum.getBlockZ() +
    " -> " +
    maximum.getBlockX() +
    ", " +
    maximum.getBlockY() +
    ", " +
    maximum.getBlockZ()
);

for (var qx = minQX; qx <= maxQX; qx++) {
    for (var qy = minQY; qy <= maxQY; qy++) {
        for (var qz = minQZ; qz <= maxQZ; qz++) {

            /*
             * Convert biome coordinates back to a representative
             * block coordinate within the 4x4x4 biome cell.
             */
            var x =
                qx * 4;

            var y =
                qy * 4;

            var z =
                qz * 4;

            examined++;

            try {
                /*
                 * Determine the chunk containing this biome cell.
                 */
                var chunkX =
                    Math.floor(x / 16);

                var chunkZ =
                    Math.floor(z / 16);

                /*
                 * Get the loaded/generated chunk.
                 *
                 * ServerLevel#getChunk(int,int)
                 * runtime name: m_6325_
                 */
                var chunk =
                    level.m_6325_(
                        chunkX,
                        chunkZ
                    );

                /*
                 * Convert world Y to the chunk section index.
                 *
                 * LevelHeightAccessor#getSectionIndex(int)
                 * runtime name: m_151564_
                 */
                var sectionIndex =
                    level.m_151564_(y);

                /*
                 * ChunkAccess#getSection(int)
                 * runtime name: m_183278_
                 */
                var section =
                    chunk.m_183278_(
                        sectionIndex
                    );

                if (section == null) {
                    skipped++;
                    continue;
                }

                /*
                 * LevelChunkSection#getBiomes()
                 * runtime name: m_187996_
                 */
                var biomeContainer =
                    section.m_187996_();

                /*
                 * Quart coordinates local to the 16x16x16 section.
                 */
                var localQX =
                    ((qx % 4) + 4) % 4;

                var localQY =
                    ((qy % 4) + 4) % 4;

                var localQZ =
                    ((qz % 4) + 4) % 4;

                /*
                 * PalettedContainer#get(int,int,int)
                 * runtime name: m_63087_
                 */
                var currentHolder =
                    biomeContainer.m_63087_(
                        localQX,
                        localQY,
                        localQZ
                    );

                /*
                 * Holder.Reference#key()
                 * runtime name: m_205785_
                 */
                var currentKey =
                    currentHolder.m_205785_();

                /*
                 * ResourceKey#location()
                 * runtime name: m_135782_
                 */
                var currentId =
                    currentKey
                        .m_135782_()
                        .toString();

                /*
                 * ONLY repair Create: Nuclear's irradiated biome.
                 */
                if (currentId != IRRADIATED_BIOME) {
                    skipped++;
                    continue;
                }

                irradiated++;

                /*
                 * Ask the world's biome generator what belongs here.
                 *
                 * ServerLevel#getUncachedNoiseBiome(...)
                 * runtime name: m_203675_
                 */
                var generatedHolder =
                    level.m_203675_(
                        qx,
                        qy,
                        qz
                    );

                /*
                 * Defensive guard.
                 */
                var generatedKey =
                    generatedHolder.m_205785_();

                var generatedId =
                    generatedKey
                        .m_135782_()
                        .toString();

                if (generatedId == IRRADIATED_BIOME) {
                    skipped++;
                    continue;
                }

                if (dryRun) {
                    restored++;
                    continue;
                }

                /*
                 * PalettedContainer#getAndSet(...)
                 * runtime name: m_63091_
                 *
                 * This writes ONLY the biome palette entry.
                 */
                biomeContainer.m_63091_(
                    localQX,
                    localQY,
                    localQZ,
                    generatedHolder
                );

                restored++;

                /*
                 * Mark the chunk dirty so the biome change is saved.
                 *
                 * ChunkAccess#setUnsaved(true)
                 * runtime name: m_8092_
                 */
                changedChunks[
                    chunkX + "," + chunkZ
                ] = chunk;

            } catch (error) {
                failures++;

                /*
                 * Do not flood chat/logs if one bad cell occurs.
                 * Report only the first 10 failures.
                 */
                if (failures <= 10) {
                    player.printError(
                        "Failed at biome cell " +
                        qx + ", " +
                        qy + ", " +
                        qz +
                        " (" +
                        x + ", " +
                        y + ", " +
                        z +
                        "): " +
                        error
                    );
                }
            }
        }
    }
}

/*
 * Mark modified chunks as needing persistence.
 */
if (!dryRun) {
    for (var chunkKey in changedChunks) {
        var chunk =
            changedChunks[chunkKey];

        chunk.m_8092_(true);
    }
}

player.print("");

player.print(
    "Biome restoration " +
    (dryRun ? "DRY RUN COMPLETE" : "COMPLETE") +
    "."
);

player.print(
    "Biome cells examined: " +
    examined
);

player.print(
    "Irradiated cells found: " +
    irradiated
);

player.print(
    "Cells restored: " +
    restored
);

player.print(
    "Cells skipped: " +
    skipped
);

player.print(
    "Failures: " +
    failures
);

player.print(
    "Blocks modified: 0"
);

if (!dryRun) {
    player.print(
        "Chunks marked for saving: " +
        Object.keys(changedChunks).length
    );
}

if (dryRun) {
    player.print(
        "No biome changes were written."
    );
}

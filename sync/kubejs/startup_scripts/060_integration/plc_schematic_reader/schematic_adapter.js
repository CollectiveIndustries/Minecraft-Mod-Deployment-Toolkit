/*
 * ============================================================
 * PLC SCHEMATIC READER - PERIPHERAL
 * ============================================================
 *
 * Minecraft:
 *     1.20.1 Forge
 *
 * KubeJS:
 *     2001.6.5-build.26
 *
 * Create:
 *     6.0.8
 *
 * KJSCC:
 *     KubeJS-CC-Tweaked
 *
 * FILE:
 *
 *     server_scripts/999_game_mechanics/
 *     plc_schematic_reader/peripheral.js
 *
 * ============================================================
 *
 * COMPUTERCRAFT API
 * ============================================================
 *
 *     ping()
 *     getBlockId()
 *     getItemId()
 *     getItemCount()
 *     hasSchematic()
 *     getItemNbt()
 *     getSchematicInfo()
 *     getSchematicFile()
 *     getSchematicData()
 *     getSchematicBlocks()
 *     getBlockRequirements()
 *     inspectInventory()
 *     _debug()
 *
 * ============================================================
 *
 * IMPORTANT
 * ============================================================
 *
 * 1. World / BlockEntity access is performed through
 *    KJSCC.mainThreadMethod().
 *
 * 2. The physical inventory is:
 *
 *       BlockEntityJS.inventory
 *
 * 3. NBT ACCESS
 *
 *    KubeJS ItemStack wrappers do not always expose NBT the
 *    same way native Minecraft ItemStacks do. This module
 *    tries MULTIPLE access paths in priority order:
 *
 *        a. stack.save(new CompoundTag())  -> looks for "tag"
 *        b. stack.getTag()
 *        c. stack.tag
 *        d. stack.nbt
 *
 *    The first path that yields a CompoundTag wins.
 *
 * 4. Actual Create schematic contents are loaded through:
 *
 *       SchematicItem.loadSchematic(level, stack)
 *
 * 5. StructureTemplate contents are serialized through:
 *
 *       StructureTemplate.save(new CompoundTag())
 *
 * 6. We deliberately DO NOT load:
 *
 *       java.nio.file.Paths
 *       java.nio.file.Files
 *
 *    KubeJS's Java class filter blocks those classes.
 *
 *    CreatePaths already provides java.nio.file.Path objects, so
 *    path operations are performed directly on those objects.
 *
 * 7. _debug() is a read-only diagnostic method. It returns a
 *    detailed snapshot of the reader's state and is safe to
 *    call at any time.
 *
 * ============================================================
 */


/* ============================================================
 * CONSTANTS
 * ============================================================
 */

var PLC_ID =
    'kubejs:plc_schematic_reader'

var SCHEMATIC_ID =
    'create:schematic'

var PLC_PERIPHERAL =
    'plc_schematic_reader'

var MAX_BLOCK_EXPORT =
    50000

var MAX_ENTITY_EXPORT =
    5000


/* ============================================================
 * JAVA CLASSES
 * ============================================================
 */

var CompoundTagClass =
    Java.loadClass(
        'net.minecraft.nbt.CompoundTag'
    )

var ListTagClass =
    Java.loadClass(
        'net.minecraft.nbt.ListTag'
    )

var NumericTagClass =
    Java.loadClass(
        'net.minecraft.nbt.NumericTag'
    )

var StringTagClass =
    Java.loadClass(
        'net.minecraft.nbt.StringTag'
    )

var ByteArrayTagClass =
    Java.loadClass(
        'net.minecraft.nbt.ByteArrayTag'
    )

var IntArrayTagClass =
    Java.loadClass(
        'net.minecraft.nbt.IntArrayTag'
    )

var LongArrayTagClass =
    Java.loadClass(
        'net.minecraft.nbt.LongArrayTag'
    )

var SchematicItemClass =
    Java.loadClass(
        'com.simibubi.create.content.schematics.SchematicItem'
    )

var CreatePathsClass =
    Java.loadClass(
        'com.simibubi.create.foundation.utility.CreatePaths'
    )


/* ============================================================
 * BASIC HELPERS
 * ============================================================
 */

function plcIsNull(value) {

    return (
        value === null
        ||
        value === undefined
    )

}


function plcIsEmpty(stack) {

    if (
        plcIsNull(
            stack
        )
    ) {

        return true

    }


    try {

        return stack.isEmpty()

    } catch (error) {

        return true

    }

}


/* ============================================================
 * ITEM ID
 * ============================================================
 */

function plcItemId(stack) {

    if (
        plcIsEmpty(
            stack
        )
    ) {

        return null

    }


    try {

        if (
            !plcIsNull(
                stack.id
            )
        ) {

            return String(
                stack.id
            )

        }

    } catch (ignored) {
    }


    try {

        var item =
            stack.getItem()

        if (
            plcIsNull(
                item
            )
        ) {

            return null

        }


        try {

            if (
                !plcIsNull(
                    item.id
                )
            ) {

                return String(
                    item.id
                )

            }

        } catch (ignored) {
        }


        return String(
            item
        )

    } catch (ignored) {
    }


    return null

}


/* ============================================================
 * SCHEMATIC CHECK
 * ============================================================
 */

function plcIsSchematic(stack) {

    try {

        return (
            plcItemId(
                stack
            )
            ===
            SCHEMATIC_ID
        )

    } catch (error) {

        return false

    }

}


/* ============================================================
 * BLOCK LEVEL / POS / ENTITY / INVENTORY
 * ============================================================
 */

function plcGetBlockLevel(block) {

    try {

        if (
            plcIsNull(
                block
            )
        ) {

            return null

        }


        return block.getLevel()

    } catch (error) {

        console.error(
            '[PLC] getLevel() failed: '
            +
            String(error)
        )

        return null

    }

}


function plcGetBlockPos(block) {

    try {

        if (
            plcIsNull(
                block
            )
        ) {

            return null

        }


        return block.getPos()

    } catch (error) {

        console.error(
            '[PLC] getPos() failed: '
            +
            String(error)
        )

        return null

    }

}


function plcGetBlockEntity(block) {

    try {

        var level =
            plcGetBlockLevel(
                block
            )

        if (
            plcIsNull(
                level
            )
        ) {

            return null

        }


        var pos =
            plcGetBlockPos(
                block
            )

        if (
            plcIsNull(
                pos
            )
        ) {

            return null

        }


        return level.getBlockEntity(
            pos
        )

    } catch (error) {

        console.error(
            '[PLC] BlockEntity lookup failed: '
            +
            String(error)
        )

        return null

    }

}


function plcGetInventory(block) {

    try {

        var entity =
            plcGetBlockEntity(
                block
            )

        if (
            plcIsNull(
                entity
            )
        ) {

            return null

        }


        var inventory =
            entity.inventory

        if (
            plcIsNull(
                inventory
            )
        ) {

            return null

        }


        return inventory

    } catch (error) {

        console.error(
            '[PLC] Inventory lookup failed: '
            +
            String(error)
        )

        return null

    }

}


function plcGetStoredStack(block) {

    try {

        var inventory =
            plcGetInventory(
                block
            )

        if (
            plcIsNull(
                inventory
            )
        ) {

            return null

        }


        var slots =
            Number(
                inventory.getSlots()
            )


        if (
            slots < 1
        ) {

            return null

        }


        var stack =
            inventory.getStackInSlot(
                0
            )


        if (
            plcIsEmpty(
                stack
            )
        ) {

            return null

        }


        return stack

    } catch (error) {

        console.error(
            '[PLC] Stored stack lookup failed: '
            +
            String(error)
        )

        return null

    }

}


/* ============================================================
 * ITEMSTACK SERIALIZATION
 * ============================================================
 */

function plcSerializeStack(stack) {

    if (
        plcIsEmpty(
            stack
        )
    ) {

        return null

    }


    try {

        return stack.save(
            new CompoundTagClass()
        )

    } catch (error) {

        console.error(
            '[PLC] ItemStack.save() failed: '
            +
            String(error)
        )

        return null

    }

}


/* ============================================================
 * ITEM TAG
 * ============================================================
 *
 * IMPORTANT:
 *
 * KubeJS ItemStack wrappers have been observed to expose NBT
 * inconsistently across versions:
 *
 *     - some builds: stack.save(tag) puts NBT under "tag"
 *     - some builds: stack.save(tag) returns INNER NBT directly
 *     - some builds: only stack.nbt / stack.tag is populated
 *     - some builds: only stack.getTag() works
 *
 * We try every path and return the first CompoundTag we get.
 *
 * Returning null here does NOT necessarily mean the schematic
 * has no NBT. It means we could not find it via any of the
 * four paths. _debug() shows which paths were attempted and
 * what each one returned.
 * ============================================================
 */

function plcGetItemTag(stack) {

    if (
        plcIsEmpty(
            stack
        )
    ) {

        return null

    }


    /* --------------------------------------------------------
     * Path 1: stack.save(new CompoundTag())
     *
     * Two sub-cases:
     *
     *     1a. result has "tag" key  -> return result.getCompound("tag")
     *     1b. result IS the inner NBT -> return result directly
     *
     * We detect 1b heuristically: if the result does NOT have
     * "tag" but does have "File" or "Owner", treat it as
     * already-inner.
     * --------------------------------------------------------
     */

    try {

        var saved =
            stack.save(
                new CompoundTagClass()
            )


        if (
            !plcIsNull(
                saved
            )
            &&
            typeof saved.contains === 'function'
        ) {

            if (
                saved.contains(
                    'tag'
                )
            ) {

                return saved.getCompound(
                    'tag'
                )

            }


            if (
                saved.contains(
                    'File'
                )
                ||
                saved.contains(
                    'Owner'
                )
            ) {

                return saved

            }

        }

    } catch (ignored) {
    }


    /* --------------------------------------------------------
     * Path 2: stack.getTag()
     * --------------------------------------------------------
     */

    try {

        if (
            typeof stack.getTag === 'function'
        ) {

            var tag =
                stack.getTag()

            if (
                !plcIsNull(
                    tag
                )
            ) {

                return tag

            }

        }

    } catch (ignored) {
    }


    /* --------------------------------------------------------
     * Path 3: stack.tag
     * --------------------------------------------------------
     */

    try {

        var propertyTag =
            stack.tag

        if (
            !plcIsNull(
                propertyTag
            )
        ) {

            return propertyTag

        }

    } catch (ignored) {
    }


    /* --------------------------------------------------------
     * Path 4: stack.nbt
     * --------------------------------------------------------
     */

    try {

        var nbtProperty =
            stack.nbt

        if (
            !plcIsNull(
                nbtProperty
            )
        ) {

            return nbtProperty

        }

    } catch (ignored) {
    }


    return null

}


/* ============================================================
 * NBT -> PLAIN JAVASCRIPT
 * ============================================================
 */

function plcNbtToPlain(tag) {

    if (
        plcIsNull(
            tag
        )
    ) {

        return null

    }


    if (
        tag instanceof CompoundTagClass
    ) {

        var object =
            {}


        try {

            var keys =
                tag.getAllKeys()

            var iterator =
                keys.iterator()

            while (
                iterator.hasNext()
            ) {

                var key =
                    String(
                        iterator.next()
                    )

                var value =
                    tag.get(
                        key
                    )

                object[key] =
                    plcNbtToPlain(
                        value
                    )

            }

        } catch (ignored) {
        }


        return object

    }


    if (
        tag instanceof ListTagClass
    ) {

        var array =
            []


        try {

            var size =
                Number(
                    tag.size()
                )


            for (
                var i = 0;
                i < size;
                i++
            ) {

                array.push(
                    plcNbtToPlain(
                        tag.get(i)
                    )
                )

            }

        } catch (ignored) {
        }


        return array

    }


    if (
        tag instanceof NumericTagClass
    ) {

        try {

            return Number(
                tag.getAsNumber()
            )

        } catch (ignored) {

            return null

        }

    }


    if (
        tag instanceof StringTagClass
    ) {

        try {

            return String(
                tag.getAsString()
            )

        } catch (ignored) {

            return null

        }

    }


    if (
        tag instanceof ByteArrayTagClass
    ) {

        try {

            var byteValues =
                tag.getAsByteArray()

            var bytes =
                []

            for (
                var b = 0;
                b < byteValues.length;
                b++
            ) {

                bytes.push(
                    Number(
                        byteValues[b]
                    )
                )

            }

            return bytes

        } catch (ignored) {

            return null

        }

    }


    if (
        tag instanceof IntArrayTagClass
    ) {

        try {

            var intValues =
                tag.getAsIntArray()

            var ints =
                []

            for (
                var x = 0;
                x < intValues.length;
                x++
            ) {

                ints.push(
                    Number(
                        intValues[x]
                    )
                )

            }

            return ints

        } catch (ignored) {

            return null

        }

    }


    if (
        tag instanceof LongArrayTagClass
    ) {

        try {

            var longValues =
                tag.getAsLongArray()

            var longs =
                []

            for (
                var y = 0;
                y < longValues.length;
                y++
            ) {

                longs.push(
                    Number(
                        longValues[y]
                    )
                )

            }

            return longs

        } catch (ignored) {

            return null

        }

    }


    try {

        return String(
            tag.getAsString()
        )

    } catch (ignored) {

        try {

            return String(
                tag
            )

        } catch (ignoredAgain) {

            return null

        }

    }

}


/* ============================================================
 * COMPLETE ITEM NBT
 * ============================================================
 */

function plcGetItemNbt(block) {

    var stack =
        plcGetStoredStack(
            block
        )

    if (
        plcIsNull(
            stack
        )
    ) {

        return null

    }


    var serialized =
        plcSerializeStack(
            stack
        )

    if (
        plcIsNull(
            serialized
        )
    ) {

        return null

    }


    return plcNbtToPlain(
        serialized
    )

}


/* ============================================================
 * SCHEMATIC METADATA
 * ============================================================
 */

function plcReadSchematicMetadata(block) {

    var stack =
        plcGetStoredStack(
            block
        )


    if (
        plcIsNull(
            stack
        )
        ||
        !plcIsSchematic(
            stack
        )
    ) {

        return null

    }


    /*
     * NBT may not exist on a blank schematic. We still return
     * metadata so the caller gets a non-null result; the
     * File/Owner fields will simply be null.
     */

    var tag =
        plcGetItemTag(
            stack
        )


    var result =
        {

            itemId:
                SCHEMATIC_ID,

            deployed:
                false,

            owner:
                null,

            file:
                null,

            rotation:
                null,

            mirror:
                null,

            anchor:
                null,

            bounds:
                null,

            hasNbt:
                !plcIsNull(
                    tag
                )

        }


    if (
        plcIsNull(
            tag
        )
    ) {

        return result

    }


    try {

        if (
            tag.contains(
                'Deployed'
            )
        ) {

            result.deployed =
                tag.getBoolean(
                    'Deployed'
                )

        }

    } catch (ignored) {
    }


    try {

        if (
            tag.contains(
                'Owner'
            )
        ) {

            result.owner =
                String(
                    tag.getString(
                        'Owner'
                    )
                )

        }

    } catch (ignored) {
    }


    try {

        if (
            tag.contains(
                'File'
            )
        ) {

            result.file =
                String(
                    tag.getString(
                        'File'
                    )
                )

        }

    } catch (ignored) {
    }


    try {

        if (
            tag.contains(
                'Rotation'
            )
        ) {

            result.rotation =
                String(
                    tag.getString(
                        'Rotation'
                    )
                )

        }

    } catch (ignored) {
    }


    try {

        if (
            tag.contains(
                'Mirror'
            )
        ) {

            result.mirror =
                String(
                    tag.getString(
                        'Mirror'
                    )
                )

        }

    } catch (ignored) {
    }


    try {

        if (
            tag.contains(
                'Anchor'
            )
        ) {

            result.anchor =
                plcNbtToPlain(
                    tag.get(
                        'Anchor'
                    )
                )

        }

    } catch (ignored) {
    }


    try {

        if (
            tag.contains(
                'Bounds'
            )
        ) {

            result.bounds =
                plcNbtToPlain(
                    tag.get(
                        'Bounds'
                    )
                )

        }

    } catch (ignored) {
    }


    return result

}


/* ============================================================
 * LOAD CREATE SCHEMATIC
 * ============================================================
 */

function plcLoadSchematic(block) {

    var stack =
        plcGetStoredStack(
            block
        )


    if (
        plcIsNull(
            stack
        )
        ||
        !plcIsSchematic(
            stack
        )
    ) {

        return null

    }


    var level =
        plcGetBlockLevel(
            block
        )


    if (
        plcIsNull(
            level
        )
    ) {

        return null

    }


    try {

        return SchematicItemClass.loadSchematic(
            level,
            stack
        )

    } catch (error) {

        console.error(
            '[PLC] SchematicItem.loadSchematic() failed: '
            +
            String(error)
        )

        return null

    }

}


/* ============================================================
 * STRUCTURE TEMPLATE SERIALIZATION
 * ============================================================
 */

function plcSerializeTemplate(template) {

    if (
        plcIsNull(
            template
        )
    ) {

        return null

    }


    try {

        return template.save(
            new CompoundTagClass()
        )

    } catch (error) {

        console.error(
            '[PLC] StructureTemplate.save() failed: '
            +
            String(error)
        )

        return null

    }

}


/* ============================================================
 * VECTOR
 * ============================================================
 */

function plcVec3i(vector) {

    if (
        plcIsNull(
            vector
        )
    ) {

        return null

    }


    try {

        return {

            x:
                Number(
                    vector.getX()
                ),

            y:
                Number(
                    vector.getY()
                ),

            z:
                Number(
                    vector.getZ()
                )

        }

    } catch (error) {

        return null

    }

}


/* ============================================================
 * STRUCTURE INFO
 * ============================================================
 */

function plcGetStructureInfo(template) {

    var info =
        {

            size:
                null,

            author:
                null

        }


    if (
        plcIsNull(
            template
        )
    ) {

        return info

    }


    try {

        info.size =
            plcVec3i(
                template.getSize()
            )

    } catch (ignored) {
    }


    try {

        info.author =
            String(
                template.getAuthor()
            )

    } catch (ignored) {
    }


    return info

}


/* ============================================================
 * PALETTE DECODING
 * ============================================================
 */

function plcDecodePaletteEntry(entry) {

    if (
        plcIsNull(
            entry
        )
        ||
        !(entry instanceof CompoundTagClass)
    ) {

        return null

    }


    var result =
        {

            id:
                null,

            properties:
                {}

        }


    try {

        if (
            entry.contains(
                'Name'
            )
        ) {

            result.id =
                String(
                    entry.getString(
                        'Name'
                    )
                )

        }

    } catch (ignored) {
    }


    try {

        if (
            entry.contains(
                'Properties'
            )
        ) {

            result.properties =
                plcNbtToPlain(
                    entry.get(
                        'Properties'
                    )
                )

        }

    } catch (ignored) {
    }


    return result

}


function plcDecodePalette(paletteTag) {

    var palette =
        []


    if (
        plcIsNull(
            paletteTag
        )
        ||
        !(paletteTag instanceof ListTagClass)
    ) {

        return palette

    }


    try {

        var size =
            Number(
                paletteTag.size()
            )


        for (
            var i = 0;
            i < size;
            i++
        ) {

            palette.push(
                plcDecodePaletteEntry(
                    paletteTag.get(
                        i
                    )
                )
            )

        }

    } catch (ignored) {
    }


    return palette

}


function plcGetPrimaryPalette(structureTag) {

    try {

        if (
            structureTag.contains(
                'palette'
            )
        ) {

            return plcDecodePalette(
                structureTag.get(
                    'palette'
                )
            )

        }

    } catch (ignored) {
    }


    try {

        if (
            structureTag.contains(
                'palettes'
            )
        ) {

            var palettes =
                structureTag.get(
                    'palettes'
                )


            if (
                palettes instanceof ListTagClass
                &&
                palettes.size() > 0
            ) {

                return plcDecodePalette(
                    palettes.get(
                        0
                    )
                )

            }

        }

    } catch (ignored) {
    }


    return []

}


/* ============================================================
 * POSITION
 * ============================================================
 */

function plcReadPosition(tag) {

    if (
        plcIsNull(
            tag
        )
        ||
        !(tag instanceof ListTagClass)
        ||
        tag.size() < 3
    ) {

        return {

            x:
                0,

            y:
                0,

            z:
                0

        }

    }


    try {

        return {

            x:
                Number(
                    tag
                        .get(0)
                        .getAsInt()
                ),

            y:
                Number(
                    tag
                        .get(1)
                        .getAsInt()
                ),

            z:
                Number(
                    tag
                        .get(2)
                        .getAsInt()
                )

        }

    } catch (error) {

        return {

            x:
                0,

            y:
                0,

            z:
                0

        }

    }

}


/* ============================================================
 * BLOCK EXTRACTION
 * ============================================================
 */

function plcExtractBlocks(structureTag) {

    var blocks =
        []


    if (
        plcIsNull(
            structureTag
        )
    ) {

        return blocks

    }


    var palette =
        plcGetPrimaryPalette(
            structureTag
        )


    var blockTag


    try {

        if (
            !structureTag.contains(
                'blocks'
            )
        ) {

            return blocks

        }


        blockTag =
            structureTag.get(
                'blocks'
            )

    } catch (ignored) {

        return blocks

    }


    if (
        plcIsNull(
            blockTag
        )
        ||
        !(blockTag instanceof ListTagClass)
    ) {

        return blocks

    }


    var total =
        Number(
            blockTag.size()
        )


    var exported =
        Math.min(
            total,
            MAX_BLOCK_EXPORT
        )


    for (
        var i = 0;
        i < exported;
        i++
    ) {

        var entry =
            blockTag.get(
                i
            )


        if (
            plcIsNull(
                entry
            )
            ||
            !(entry instanceof CompoundTagClass)
        ) {

            continue

        }


        var stateIndex =
            0


        var position =
            {

                x:
                    0,

                y:
                    0,

                z:
                    0

            }


        var blockId =
            null


        var properties =
            {}


        var blockNbt =
            null


        try {

            if (
                entry.contains(
                    'pos'
                )
            ) {

                position =
                    plcReadPosition(
                        entry.get(
                            'pos'
                        )
                    )

            }

        } catch (ignored) {
        }


        try {

            if (
                entry.contains(
                    'state'
                )
            ) {

                stateIndex =
                    Number(
                        entry.getInt(
                            'state'
                        )
                    )

            }

        } catch (ignored) {
        }


        if (
            stateIndex >= 0
            &&
            stateIndex < palette.length
        ) {

            var state =
                palette[
                    stateIndex
                ]


            if (
                !plcIsNull(
                    state
                )
            ) {

                blockId =
                    state.id

                properties =
                    state.properties

            }

        }


        try {

            if (
                entry.contains(
                    'nbt'
                )
            ) {

                blockNbt =
                    plcNbtToPlain(
                        entry.get(
                            'nbt'
                        )
                    )

            }

        } catch (ignored) {
        }


        blocks.push(
            {

                index:
                    i,

                stateIndex:
                    stateIndex,

                id:
                    blockId,

                properties:
                    properties,

                pos:
                    position,

                nbt:
                    blockNbt

            }
        )

    }


    return blocks

}


/* ============================================================
 * ENTITY EXTRACTION
 * ============================================================
 */

function plcExtractEntities(structureTag) {

    var entities =
        []


    if (
        plcIsNull(
            structureTag
        )
    ) {

        return entities

    }


    var entityTag


    try {

        if (
            !structureTag.contains(
                'entities'
            )
        ) {

            return entities

        }


        entityTag =
            structureTag.get(
                'entities'
            )

    } catch (ignored) {

        return entities

    }


    if (
        plcIsNull(
            entityTag
        )
        ||
        !(entityTag instanceof ListTagClass)
    ) {

        return entities

    }


    var total =
        Number(
            entityTag.size()
        )


    var exported =
        Math.min(
            total,
            MAX_ENTITY_EXPORT
        )


    for (
        var i = 0;
        i < exported;
        i++
    ) {

        try {

            entities.push(
                plcNbtToPlain(
                    entityTag.get(
                        i
                    )
                )
            )

        } catch (ignored) {
        }

    }


    return entities

}


/* ============================================================
 * REQUIREMENTS
 * ============================================================
 */

function plcBuildRequirements(blocks) {

    var requirements =
        {}


    for (
        var i = 0;
        i < blocks.length;
        i++
    ) {

        var block =
            blocks[i]


        if (
            plcIsNull(
                block
            )
            ||
            plcIsNull(
                block.id
            )
            ||
            block.id === ''
        ) {

            continue

        }


        var id =
            String(
                block.id
            )


        if (
            requirements[id]
            ===
            undefined
        ) {

            requirements[id] =
                0

        }


        requirements[id]++

    }


    return requirements

}


/* ============================================================
 * SCHEMATIC FILE PATH
 * ============================================================
 */

function plcResolveSchematicPath(
    level,
    metadata
) {

    if (
        plcIsNull(
            level
        )
        ||
        plcIsNull(
            metadata
        )
        ||
        plcIsNull(
            metadata.file
        )
        ||
        metadata.file === ''
    ) {

        return null

    }


    try {

        var base


        var resolved


        if (
            !level.isClientSide()
        ) {

            if (
                plcIsNull(
                    metadata.owner
                )
                ||
                metadata.owner === ''
            ) {

                return null

            }


            base =
                CreatePathsClass
                    .UPLOADED_SCHEMATICS_DIR


            resolved =
                base
                    .resolve(
                        metadata.owner
                    )
                    .resolve(
                        metadata.file
                    )
                    .normalize()

        }

        else {

            base =
                CreatePathsClass
                    .SCHEMATICS_DIR


            resolved =
                base
                    .resolve(
                        metadata.file
                    )
                    .normalize()

        }


        var normalizedBase =
            base
                .toAbsolutePath()
                .normalize()


        var normalizedResolved =
            resolved
                .toAbsolutePath()
                .normalize()


        if (
            !normalizedResolved.startsWith(
                normalizedBase
            )
        ) {

            console.warn(
                '[PLC] Rejected schematic path outside Create schematic directory.'
            )

            return null

        }


        return normalizedResolved

    } catch (error) {

        console.error(
            '[PLC] Schematic path resolution failed: '
            +
            String(error)
        )

        return null

    }

}


/* ============================================================
 * SCHEMATIC FILE INFORMATION
 * ============================================================
 */

function plcGetSchematicFileInfo(block) {

    var metadata =
        plcReadSchematicMetadata(
            block
        )


    if (
        plcIsNull(
            metadata
        )
    ) {

        return null

    }


    var level =
        plcGetBlockLevel(
            block
        )


    if (
        plcIsNull(
            level
        )
    ) {

        return null

    }


    var result =
        {

            file:
                metadata.file,

            owner:
                metadata.owner,

            deployed:
                metadata.deployed,

            hasNbt:
                metadata.hasNbt,

            exists:
                false,

            sizeBytes:
                0

        }


    var path =
        plcResolveSchematicPath(
            level,
            metadata
        )


    if (
        plcIsNull(
            path
        )
    ) {

        return result

    }


    try {

        var fileObject =
            path.toFile()


        result.exists =
            fileObject.exists()


        if (
            result.exists
        ) {

            result.sizeBytes =
                Number(
                    fileObject.length()
                )

        }

    } catch (error) {

        console.warn(
            '[PLC] Schematic file metadata inspection failed: '
            +
            String(error)
        )

    }


    return result

}


/* ============================================================
 * SCHEMATIC DATA
 * ============================================================
 */

function plcGetSchematicData(block) {

    try {

        var stack =
            plcGetStoredStack(
                block
            )


        if (
            plcIsNull(
                stack
            )
            ||
            !plcIsSchematic(
                stack
            )
        ) {

            return null

        }


        var metadata =
            plcReadSchematicMetadata(
                block
            )


        var template =
            plcLoadSchematic(
                block
            )


        if (
            plcIsNull(
                template
            )
        ) {

            return {

                loaded:
                    false,

                file:
                    metadata
                        ? metadata.file
                        : null,

                owner:
                    metadata
                        ? metadata.owner
                        : null,

                hasNbt:
                    metadata
                        ? metadata.hasNbt
                        : false,

                error:
                    'Create SchematicItem.loadSchematic() returned no template.'

            }

        }


        var structureTag =
            plcSerializeTemplate(
                template
            )


        if (
            plcIsNull(
                structureTag
            )
        ) {

            return {

                loaded:
                    false,

                file:
                    metadata
                        ? metadata.file
                        : null,

                owner:
                    metadata
                        ? metadata.owner
                        : null,

                hasNbt:
                    metadata
                        ? metadata.hasNbt
                        : false,

                error:
                    'StructureTemplate.save() returned null.'

            }

        }


        var structureInfo =
            plcGetStructureInfo(
                template
            )


        var blocks =
            plcExtractBlocks(
                structureTag
            )


        var entities =
            plcExtractEntities(
                structureTag
            )


        var requirements =
            plcBuildRequirements(
                blocks
            )


        var actualBlockCount =
            0


        var actualEntityCount =
            0


        try {

            if (
                structureTag.contains(
                    'blocks'
                )
            ) {

                actualBlockCount =
                    Number(
                        structureTag
                            .getList(
                                'blocks',
                                10
                            )
                            .size()
                    )

            }

        } catch (ignored) {
        }


        try {

            if (
                structureTag.contains(
                    'entities'
                )
            ) {

                actualEntityCount =
                    Number(
                        structureTag
                            .getList(
                                'entities',
                                10
                            )
                            .size()
                    )

            }

        } catch (ignored) {
        }


        return {

            loaded:
                true,

            file:
                metadata
                    ? metadata.file
                    : null,

            owner:
                metadata
                    ? metadata.owner
                    : null,

            deployed:
                metadata
                    ? metadata.deployed
                    : false,

            hasNbt:
                metadata
                    ? metadata.hasNbt
                    : false,

            author:
                structureInfo.author,

            size:
                structureInfo.size,

            blockCount:
                blocks.length,

            entityCount:
                entities.length,

            blocks:
                blocks,

            entities:
                entities,

            requirements:
                requirements,

            truncated:
                actualBlockCount
                >
                MAX_BLOCK_EXPORT,

            entityTruncated:
                actualEntityCount
                >
                MAX_ENTITY_EXPORT

        }

    } catch (error) {

        console.error(
            '[PLC] getSchematicData failed: '
            +
            String(error)
        )

        return {

            loaded:
                false,

            error:
                String(error)

        }

    }

}


/* ============================================================
 * SCHEMATIC INFO
 * ============================================================
 */

function plcGetSchematicInfo(block) {

    var metadata =
        plcReadSchematicMetadata(
            block
        )


    if (
        plcIsNull(
            metadata
        )
    ) {

        return null

    }


    var template =
        plcLoadSchematic(
            block
        )


    var result =
        {

            itemId:
                metadata.itemId,

            deployed:
                metadata.deployed,

            owner:
                metadata.owner,

            file:
                metadata.file,

            rotation:
                metadata.rotation,

            mirror:
                metadata.mirror,

            anchor:
                metadata.anchor,

            bounds:
                metadata.bounds,

            hasNbt:
                metadata.hasNbt,

            author:
                null,

            size:
                null

        }


    if (
        !plcIsNull(
            template
        )
    ) {

        var structureInfo =
            plcGetStructureInfo(
                template
            )


        result.author =
            structureInfo.author


        result.size =
            structureInfo.size

    }


    return result

}


/* ============================================================
 * INVENTORY DIAGNOSTICS
 * ============================================================
 */

function plcInspectInventory(block) {

    try {

        var entity =
            plcGetBlockEntity(
                block
            )


        var inventory =
            plcGetInventory(
                block
            )


        var stack =
            plcGetStoredStack(
                block
            )


        var result =
            {

                blockId:
                    PLC_ID,

                entityFound:
                    !plcIsNull(
                        entity
                    ),

                inventoryFound:
                    !plcIsNull(
                        inventory
                    ),

                slots:
                    0,

                itemId:
                    null,

                count:
                    0,

                hasSchematic:
                    false

            }


        if (
            !plcIsNull(
                entity
            )
        ) {

            try {

                result.entityClass =
                    String(
                        entity
                            .getClass()
                            .getName()
                    )

            } catch (ignored) {
            }

        }


        if (
            !plcIsNull(
                inventory
            )
        ) {

            try {

                result.inventoryClass =
                    String(
                        inventory
                            .getClass()
                            .getName()
                    )

            } catch (ignored) {
            }


            try {

                result.slots =
                    Number(
                        inventory.getSlots()
                    )

            } catch (ignored) {
            }

        }


        if (
            !plcIsEmpty(
                stack
            )
        ) {

            result.itemId =
                plcItemId(
                    stack
                )


            try {

                result.count =
                    Number(
                        stack.getCount()
                    )

            } catch (ignored) {
            }


            result.hasSchematic =
                plcIsSchematic(
                    stack
                )

        }


        return result

    } catch (error) {

        console.error(
            '[PLC] inspectInventory failed: '
            +
            String(error)
        )

        return {

            error:
                String(error)

        }

    }

}


/* ============================================================
 * DEBUG DIAGNOSTIC
 * ============================================================
 *
 * Returns a detailed snapshot of everything the reader can
 * see about the currently stored item. Used by the PLC to
 * diagnose why metadata extraction is failing.
 *
 * This method NEVER raises. Every path is individually
 * pcall'd / try/catch'd so partial failures still produce
 * a usable report.
 * ============================================================
 */

function plcDebug(block) {

    var report =
        {

            blockId:
                PLC_ID,

            ok:
                true

        }


    /* --------------------------------------------------------
     * ENTITY / INVENTORY
     * --------------------------------------------------------
     */

    try {

        var entity =
            plcGetBlockEntity(
                block
            )

        report.entityFound =
            !plcIsNull(
                entity
            )


        if (
            !plcIsNull(
                entity
            )
        ) {

            try {

                report.entityClass =
                    String(
                        entity
                            .getClass()
                            .getName()
                    )

            } catch (ignored) {
            }

        }

    } catch (error) {

        report.entityError =
            String(
                error
            )

    }


    try {

        var inventory =
            plcGetInventory(
                block
            )

        report.inventoryFound =
            !plcIsNull(
                inventory
            )


        if (
            !plcIsNull(
                inventory
            )
        ) {

            try {

                report.inventoryClass =
                    String(
                        inventory
                            .getClass()
                            .getName()
                    )

            } catch (ignored) {
            }


            try {

                report.inventorySlots =
                    Number(
                        inventory.getSlots()
                    )

            } catch (ignored) {
            }

        }

    } catch (error) {

        report.inventoryError =
            String(
                error
            )

    }


    /* --------------------------------------------------------
     * STORED STACK
     * --------------------------------------------------------
     */

    var stack =
        null


    try {

        stack =
            plcGetStoredStack(
                block
            )

        report.stackFound =
            !plcIsNull(
                stack
            )

    } catch (error) {

        report.stackError =
            String(
                error
            )

    }


    if (
        plcIsNull(
            stack
        )
    ) {

        return report

    }


    /* --------------------------------------------------------
     * STACK IDENTITY
     * --------------------------------------------------------
     */

    try {

        report.stackId =
            plcItemId(
                stack
            )

    } catch (error) {

        report.stackIdError =
            String(
                error
            )

    }


    try {

        report.stackCount =
            Number(
                stack.getCount()
            )

    } catch (error) {

        report.stackCountError =
            String(
                error
            )

    }


    try {

        report.stackClass =
            String(
                stack
                    .getClass()
                    .getName()
            )

    } catch (ignored) {
    }


    /* --------------------------------------------------------
     * PROPERTY PROBES
     * --------------------------------------------------------
     */

    try {

        var probeId =
            stack.id

        report.probeDotId =
            plcIsNull(
                probeId
            )
                ? null
                : String(
                    probeId
                )

    } catch (error) {

        report.probeDotIdError =
            String(
                error
            )

    }


    try {

        var probeNbt =
            stack.nbt

        report.probeDotNbt =
            plcIsNull(
                probeNbt
            )
                ? null
                : String(
                    probeNbt
                )

    } catch (error) {

        report.probeDotNbtError =
            String(
                error
            )

    }


    try {

        var probeTag =
            stack.tag

        report.probeDotTag =
            plcIsNull(
                probeTag
            )
                ? null
                : String(
                    probeTag
                )

    } catch (error) {

        report.probeDotTagError =
            String(
                error
            )

    }


    /* --------------------------------------------------------
     * METHOD PROBES
     * --------------------------------------------------------
     */

    try {

        report.hasGetTagMethod =
            typeof stack.getTag === 'function'

    } catch (error) {

        report.hasGetTagMethodError =
            String(
                error
            )

    }


    try {

        if (
            typeof stack.getTag === 'function'
        ) {

            var methodTag =
                stack.getTag()

            report.getTagResult =
                plcIsNull(
                    methodTag
                )
                    ? null
                    : String(
                        methodTag
                    )

        }

    } catch (error) {

        report.getTagError =
            String(
                error
            )

    }


    try {

        report.hasSaveMethod =
            typeof stack.save === 'function'

    } catch (error) {

        report.hasSaveMethodError =
            String(
                error
            )

    }


    /* --------------------------------------------------------
     * SAVE PROBE
     * --------------------------------------------------------
     */

    try {

        var saved =
            stack.save(
                new CompoundTagClass()
            )


        report.saveResultType =
            typeof saved


        report.saveIsCompound =
            saved instanceof CompoundTagClass


        if (
            report.saveIsCompound
        ) {

            try {

                var keys =
                    []

                var iterator =
                    saved
                        .getAllKeys()
                        .iterator()

                while (
                    iterator.hasNext()
                ) {

                    keys.push(
                        String(
                            iterator.next()
                        )
                    )

                }

                report.saveKeys =
                    keys

            } catch (error) {

                report.saveKeysError =
                    String(
                        error
                    )

            }


            try {

                report.saveContainsTag =
                    saved.contains(
                        'tag'
                    )

            } catch (error) {

                report.saveContainsTagError =
                    String(
                        error
                    )

            }


            try {

                report.saveContainsFile =
                    saved.contains(
                        'File'
                    )

            } catch (ignored) {
            }


            try {

                report.saveContainsOwner =
                    saved.contains(
                        'Owner'
                    )

            } catch (ignored) {
            }

        } else {

            report.saveStringified =
                String(
                    saved
                )

        }

    } catch (error) {

        report.saveError =
            String(
                error
            )

    }


    /* --------------------------------------------------------
     * ITEM TAG PROBE (via plcGetItemTag)
     * --------------------------------------------------------
     */

    try {

        var resolvedTag =
            plcGetItemTag(
                stack
            )

        report.resolvedTagFound =
            !plcIsNull(
                resolvedTag
            )


        if (
            !plcIsNull(
                resolvedTag
            )
        ) {

            try {

                var tagKeys =
                    []

                var tagIterator =
                    resolvedTag
                        .getAllKeys()
                        .iterator()

                while (
                    tagIterator.hasNext()
                ) {

                    tagKeys.push(
                        String(
                            tagIterator.next()
                        )
                    )

                }

                report.resolvedTagKeys =
                    tagKeys

            } catch (error) {

                report.resolvedTagKeysError =
                    String(
                        error
                    )

            }

        }

    } catch (error) {

        report.resolvedTagError =
            String(
                error
            )

    }


    /* --------------------------------------------------------
     * LOAD SCHEMATIC PROBE
     * --------------------------------------------------------
     */

    try {

        var template =
            plcLoadSchematic(
                block
            )

        report.templateLoaded =
            !plcIsNull(
                template
            )


        if (
            !plcIsNull(
                template
            )
        ) {

            try {

                var info =
                    plcGetStructureInfo(
                        template
                    )

                report.templateAuthor =
                    info.author

                report.templateSize =
                    info.size

            } catch (error) {

                report.templateInfoError =
                    String(
                        error
                    )

            }

        }

    } catch (error) {

        report.templateLoadError =
            String(
                error
            )

    }


    return report

}


/* ============================================================
 * PERIPHERAL REGISTRATION
 * ============================================================
 */

ComputerCraftEvents.peripheral(
    function(event) {

        var peripheral =
            event.registerPeripheral(
                PLC_PERIPHERAL,
                PLC_ID
            )


        /* ====================================================
         * ping()
         * ====================================================
         */

        peripheral.method(
            'ping',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                return 'PLC SCHEMATIC READER OK'

            }
        )


        /* ====================================================
         * getBlockId()
         * ====================================================
         */

        peripheral.method(
            'getBlockId',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                return PLC_ID

            }
        )


        /* ====================================================
         * getItemId()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getItemId',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    return plcItemId(
                        plcGetStoredStack(
                            block
                        )
                    )

                } catch (error) {

                    console.error(
                        '[PLC] getItemId failed: '
                        +
                        String(error)
                    )

                    return null

                }

            }
        )


        /* ====================================================
         * getItemCount()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getItemCount',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    var stack =
                        plcGetStoredStack(
                            block
                        )


                    if (
                        plcIsEmpty(
                            stack
                        )
                    ) {

                        return 0

                    }


                    return Number(
                        stack.getCount()
                    )

                } catch (error) {

                    console.error(
                        '[PLC] getItemCount failed: '
                        +
                        String(error)
                    )

                    return 0

                }

            }
        )


        /* ====================================================
         * hasSchematic()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'hasSchematic',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    return plcIsSchematic(
                        plcGetStoredStack(
                            block
                        )
                    )

                } catch (error) {

                    console.error(
                        '[PLC] hasSchematic failed: '
                        +
                        String(error)
                    )

                    return false

                }

            }
        )


        /* ====================================================
         * getItemNbt()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getItemNbt',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    return plcGetItemNbt(
                        block
                    )

                } catch (error) {

                    console.error(
                        '[PLC] getItemNbt failed: '
                        +
                        String(error)
                    )

                    return null

                }

            }
        )


        /* ====================================================
         * getSchematicInfo()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getSchematicInfo',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    return plcGetSchematicInfo(
                        block
                    )

                } catch (error) {

                    console.error(
                        '[PLC] getSchematicInfo failed: '
                        +
                        String(error)
                    )

                    return null

                }

            }
        )


        /* ====================================================
         * getSchematicFile()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getSchematicFile',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    return plcGetSchematicFileInfo(
                        block
                    )

                } catch (error) {

                    console.error(
                        '[PLC] getSchematicFile failed: '
                        +
                        String(error)
                    )

                    return null

                }

            }
        )


        /* ====================================================
         * getSchematicData()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getSchematicData',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                return plcGetSchematicData(
                    block
                )

            }
        )


        /* ====================================================
         * getSchematicBlocks()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getSchematicBlocks',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    var data =
                        plcGetSchematicData(
                            block
                        )


                    if (
                        plcIsNull(
                            data
                        )
                    ) {

                        return null

                    }


                    if (
                        data.loaded
                        !==
                        true
                    ) {

                        return data

                    }


                    return {

                        size:
                            data.size,

                        blockCount:
                            data.blockCount,

                        truncated:
                            data.truncated,

                        blocks:
                            data.blocks

                    }

                } catch (error) {

                    console.error(
                        '[PLC] getSchematicBlocks failed: '
                        +
                        String(error)
                    )

                    return null

                }

            }
        )


        /* ====================================================
         * getBlockRequirements()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'getBlockRequirements',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                try {

                    var data =
                        plcGetSchematicData(
                            block
                        )


                    if (
                        plcIsNull(
                            data
                        )
                    ) {

                        return null

                    }


                    if (
                        data.loaded
                        !==
                        true
                    ) {

                        return data

                    }


                    return data.requirements

                } catch (error) {

                    console.error(
                        '[PLC] getBlockRequirements failed: '
                        +
                        String(error)
                    )

                    return null

                }

            }
        )


        /* ====================================================
         * inspectInventory()
         * ====================================================
         */

        peripheral.mainThreadMethod(
            'inspectInventory',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                return plcInspectInventory(
                    block
                )

            }
        )


        /* ====================================================
         * _debug()
         * ====================================================
         *
         * Read-only diagnostic. Never raises. Safe to call
         * at any time, including from outside the PLC.
         */

        peripheral.mainThreadMethod(
            '_debug',

            function(
                block,
                side,
                arguments,
                computer,
                context
            ) {

                return plcDebug(
                    block
                )

            }
        )


        console.info(
            '[PLC] KJSCC peripheral registered: '
            +
            PLC_PERIPHERAL
        )

    }
)


/* ============================================================
 * STARTUP REPORT
 * ============================================================
 */

console.info(
    '[PLC] ============================================'
)

console.info(
    '[PLC] PLC SCHEMATIC READER PERIPHERAL'
)

console.info(
    '[PLC] Block: '
    +
    PLC_ID
)

console.info(
    '[PLC] Peripheral: '
    +
    PLC_PERIPHERAL
)

console.info(
    '[PLC] Accepted item: '
    +
    SCHEMATIC_ID
)

console.info(
    '[PLC] Inventory path: BlockEntityJS.inventory'
)

console.info(
    '[PLC] Inventory type: InventoryAttachment'
)

console.info(
    '[PLC] Inventory slots: 1'
)

console.info(
    '[PLC] KJSCC state access: mainThreadMethod()'
)

console.info(
    '[PLC] Create loader: SchematicItem.loadSchematic()'
)

console.info(
    '[PLC] Structure serialization: StructureTemplate.save()'
)

console.info(
    '[PLC] NBT conversion: recursive NBT -> plain JS'
)

console.info(
    '[PLC] NBT access paths: save/tag, getTag, .tag, .nbt'
)

console.info(
    '[PLC] Block extraction: palette + blocks'
)

console.info(
    '[PLC] Entity extraction: entities'
)

console.info(
    '[PLC] Requirement aggregation: enabled'
)

console.info(
    '[PLC] Maximum exported blocks: '
    +
    MAX_BLOCK_EXPORT
)

console.info(
    '[PLC] Maximum exported entities: '
    +
    MAX_ENTITY_EXPORT
)

console.info(
    '[PLC] File path resolution: Create CreatePaths'
)

console.info(
    '[PLC] Java NIO helper classes: NOT loaded'
)

console.info(
    '[PLC] Methods:'
)

console.info(
    '[PLC]   ping'
)

console.info(
    '[PLC]   getBlockId'
)

console.info(
    '[PLC]   getItemId'
)

console.info(
    '[PLC]   getItemCount'
)

console.info(
    '[PLC]   hasSchematic'
)

console.info(
    '[PLC]   getItemNbt'
)

console.info(
    '[PLC]   getSchematicInfo'
)

console.info(
    '[PLC]   getSchematicFile'
)

console.info(
    '[PLC]   getSchematicData'
)

console.info(
    '[PLC]   getSchematicBlocks'
)

console.info(
    '[PLC]   getBlockRequirements'
)

console.info(
    '[PLC]   inspectInventory'
)

console.info(
    '[PLC]   _debug'
)

console.info(
    '[PLC] ============================================'
)

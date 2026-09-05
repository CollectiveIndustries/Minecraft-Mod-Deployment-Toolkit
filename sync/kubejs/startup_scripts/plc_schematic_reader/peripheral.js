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
 * 3. ItemStack NBT is serialized using:
 *
 *       stack.save(new CompoundTag())
 *
 *    We do NOT call stack.getTag().
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
 *
 * ONLY classes known to be permitted by the KubeJS class filter
 * are loaded here.
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


    /*
     * KubeJS ItemStack wrapper.
     *
     * This path is known to work with this environment.
     */

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


    /*
     * Native ItemStack fallback.
     */

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
 * BLOCK LEVEL
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


/* ============================================================
 * BLOCK POSITION
 * ============================================================
 */

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


/* ============================================================
 * BLOCK ENTITY
 * ============================================================
 */

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


/* ============================================================
 * INVENTORY
 * ============================================================
 */

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


/* ============================================================
 * STORED STACK
 * ============================================================
 */

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
 */

function plcGetItemTag(stack) {

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


    try {

        if (
            !serialized.contains(
                'tag'
            )
        ) {

            return null

        }


        return serialized.getCompound(
            'tag'
        )

    } catch (error) {

        console.error(
            '[PLC] Item tag extraction failed: '
            +
            String(error)
        )

        return null

    }

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


    /* --------------------------------------------------------
     * CompoundTag
     * --------------------------------------------------------
     */

    if (
        tag instanceof CompoundTagClass
    ) {

        var object =
            {}

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

        return object

    }


    /* --------------------------------------------------------
     * ListTag
     * --------------------------------------------------------
     */

    if (
        tag instanceof ListTagClass
    ) {

        var array =
            []

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

        return array

    }


    /* --------------------------------------------------------
     * NumericTag
     * --------------------------------------------------------
     */

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


    /* --------------------------------------------------------
     * StringTag
     * --------------------------------------------------------
     */

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


    /* --------------------------------------------------------
     * ByteArrayTag
     * --------------------------------------------------------
     */

    if (
        tag instanceof ByteArrayTagClass
    ) {

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

    }


    /* --------------------------------------------------------
     * IntArrayTag
     * --------------------------------------------------------
     */

    if (
        tag instanceof IntArrayTagClass
    ) {

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

    }


    /* --------------------------------------------------------
     * LongArrayTag
     * --------------------------------------------------------
     */

    if (
        tag instanceof LongArrayTagClass
    ) {

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

    }


    /* --------------------------------------------------------
     * Fallback
     * --------------------------------------------------------
     */

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


    var tag =
        plcGetItemTag(
            stack
        )

    if (
        plcIsNull(
            tag
        )
    ) {

        return null

    }


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
                null

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


/* ============================================================
 * PALETTE
 * ============================================================
 */

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


    return palette

}


/* ============================================================
 * PRIMARY PALETTE
 * ============================================================
 */

function plcGetPrimaryPalette(structureTag) {

    /*
     * Standard Minecraft structure format.
     */

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


    /*
     * Multi-palette compatibility.
     */

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


        /*
         * Position
         */

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


        /*
         * State
         */

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


        /*
         * Palette lookup
         */

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


        /*
         * Block entity NBT
         */

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
 *
 * IMPORTANT:
 *
 * We do NOT use:
 *
 *     Java.loadClass('java.nio.file.Paths')
 *     Java.loadClass('java.nio.file.Files')
 *
 * Both are blocked by KubeJS's class filter.
 *
 * CreatePaths already exposes Path objects:
 *
 *     CreatePaths.UPLOADED_SCHEMATICS_DIR
 *     CreatePaths.SCHEMATICS_DIR
 *
 * Java Path itself supports:
 *
 *     resolve()
 *     normalize()
 *     toAbsolutePath()
 *     startsWith()
 *
 * Therefore we use the returned Path directly.
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


        /*
         * SERVER
         *
         * Create source:
         *
         *     dir = CreatePaths.UPLOADED_SCHEMATICS_DIR
         *     file = Paths.get(owner, schematic)
         */

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


            /*
             * Equivalent to:
             *
             *     base.resolve(
             *         Paths.get(owner, file)
             *     )
             *
             * without loading java.nio.file.Paths.
             */

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

        /*
         * CLIENT
         */

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


        /*
         * Normalize both paths before comparison.
         */

        var normalizedBase =
            base
                .toAbsolutePath()
                .normalize()


        var normalizedResolved =
            resolved
                .toAbsolutePath()
                .normalize()


        /*
         * Prevent path traversal.
         *
         * This matches the protection Create performs in
         * SchematicItem.loadSchematic().
         */

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
 *
 * We use the exact same path construction as Create.
 *
 * Exposed to Lua:
 *
 *     file
 *     owner
 *     deployed
 *     exists
 *     sizeBytes
 *
 * No absolute filesystem path is exposed.
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


    /*
     * We already possess a java.nio.file.Path object here.
     *
     * We do not load java.nio.file.Files.
     *
     * Path#toFile() gives us a File object and allows normal
     * existence/size inspection without Java.loadClass().
     */

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

        /*
         * File inspection is informational only.
         * Actual schematic loading is still performed by
         * Create's SchematicItem.loadSchematic().
         */

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
    '[PLC] ============================================'
)

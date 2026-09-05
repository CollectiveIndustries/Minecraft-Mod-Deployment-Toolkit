/*
 * ============================================================
 * PLC SCHEMATIC READER
 * SERVER INTERACTION
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
 * PURPOSE
 * -------
 *
 * Implements the physical reader behavior:
 *
 *     Empty hand + schematic in reader:
 *         Reader -> player
 *
 *     Schematic in hand + empty reader:
 *         Player -> reader
 *
 *     Schematic in hand + schematic in reader:
 *         Old -> player
 *         New -> reader
 *
 * Only TOP face interaction is accepted.
 *
 * IMPORTANT
 * ---------
 *
 * ForgeEvents is NOT available from this server_scripts
 * environment.
 *
 * Therefore we register directly against:
 *
 *     MinecraftForge.EVENT_BUS
 *
 * using an explicit java.util.function.Consumer.
 * ============================================================
 */


/* ============================================================
 * JAVA CLASSES
 * ============================================================
 */

var ItemStackClass =
    Java.loadClass(
        'net.minecraft.world.item.ItemStack'
    )

var DirectionClass =
    Java.loadClass(
        'net.minecraft.core.Direction'
    )

var InteractionHandClass =
    Java.loadClass(
        'net.minecraft.world.InteractionHand'
    )

var InteractionResultClass =
    Java.loadClass(
        'net.minecraft.world.InteractionResult'
    )

var MinecraftForgeClass =
    Java.loadClass(
        'net.minecraftforge.common.MinecraftForge'
    )

var EventPriorityClass =
    Java.loadClass(
        'net.minecraftforge.eventbus.api.EventPriority'
    )

var RightClickBlockEventClass =
    Java.loadClass(
        'net.minecraftforge.event.entity.player.PlayerInteractEvent$RightClickBlock'
    )

var ConsumerClass =
    Java.loadClass(
        'java.util.function.Consumer'
    )


/* ============================================================
 * CONSTANTS
 * ============================================================
 */

var PLC_ID =
    'kubejs:plc_schematic_reader'

var SCHEMATIC_ID =
    'create:schematic'


/* ============================================================
 * ITEM HELPERS
 * ============================================================
 */

function plcIsEmpty(stack) {

    try {

        return (
            stack === null
            ||
            stack === undefined
            ||
            stack.isEmpty()
        )

    } catch (error) {

        return true

    }

}


function plcItemId(stack) {

    try {

        if (
            plcIsEmpty(
                stack
            )
        ) {

            return null

        }


        /*
         * KubeJS exposes ItemStack.id.
         */

        return String(
            stack.id
        )

    } catch (error) {

        try {

            return String(
                stack.getItem()
            )

        } catch (ignored) {

            return null

        }

    }

}


function plcIsSchematic(stack) {

    return (
        plcItemId(
            stack
        )
        ===
        SCHEMATIC_ID
    )

}


/* ============================================================
 * BLOCK TEST
 * ============================================================
 */

function plcIsReaderBlock(
    level,
    pos
) {

    try {

        if (
            !level
            ||
            !pos
        ) {

            return false

        }


        var state =
            level.getBlockState(
                pos
            )


        if (!state) {

            return false

        }


        /*
         * Preferred KubeJS registry ID.
         */

        try {

            if (
                String(
                    state.getBlock().id
                )
                ===
                PLC_ID
            ) {

                return true

            }

        } catch (ignored) {
        }


        /*
         * Fallback.
         */

        try {

            return (
                String(
                    state.getBlock()
                )
                ===
                'Block{' + PLC_ID + '}'
            )

        } catch (ignored) {

            return false

        }

    } catch (error) {

        return false

    }

}


/* ============================================================
 * PLAYER INVENTORY / BLOCK ENTITY
 * ============================================================
 */

function plcGetBlockEntity(
    level,
    pos
) {

    try {

        if (
            !level
            ||
            !pos
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
 * REGISTERED INVENTORY ACCESS
 * ============================================================
 */

function plcGetInventory(
    blockEntity
) {

    try {

        if (!blockEntity) {

            return null

        }


        /*
         * Verified KubeJS path:
         *
         *     BlockEntityJS.inventory
         */

        var inventory =
            blockEntity.inventory


        if (!inventory) {

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
 * INSERT / EXTRACT HELPERS
 * ============================================================
 */

function plcExtractStored(
    inventory,
    count
) {

    try {

        return inventory.extractItem(
            0,
            count,
            false
        )

    } catch (error) {

        console.error(
            '[PLC] Inventory extraction failed: '
            +
            String(error)
        )

        return null

    }

}


function plcInsertStored(
    inventory,
    stack
) {

    try {

        return inventory.insertItem(
            0,
            stack,
            false
        )

    } catch (error) {

        console.error(
            '[PLC] Inventory insertion failed: '
            +
            String(error)
        )

        return null

    }

}


/* ============================================================
 * RIGHT CLICK HANDLER
 * ============================================================
 */

function plcHandleRightClick(
    event
) {

    try {

        /*
         * ------------------------------------------------------
         * PLAYER
         * ------------------------------------------------------
         */

        var player =
            event.getEntity()


        if (!player) {

            return

        }


        /*
         * ------------------------------------------------------
         * MAIN HAND ONLY
         * ------------------------------------------------------
         */

        if (
            event.getHand()
            !==
            InteractionHandClass.MAIN_HAND
        ) {

            return

        }


        /*
         * ------------------------------------------------------
         * LEVEL / POSITION
         * ------------------------------------------------------
         */

        var level =
            event.getLevel()

        var pos =
            event.getPos()


        if (
            !level
            ||
            !pos
        ) {

            return

        }


        /*
         * ------------------------------------------------------
         * ONLY OUR BLOCK
         * ------------------------------------------------------
         */

        if (
            !plcIsReaderBlock(
                level,
                pos
            )
        ) {

            return

        }


        /*
         * ------------------------------------------------------
         * TOP FACE ONLY
         * ------------------------------------------------------
         */

        if (
            event.getFace()
            !==
            DirectionClass.UP
        ) {

            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        /*
         * ------------------------------------------------------
         * SERVER ONLY
         * ------------------------------------------------------
         */

        if (
            level.isClientSide()
        ) {

            return

        }


        /*
         * ------------------------------------------------------
         * BLOCK ENTITY
         * ------------------------------------------------------
         */

        var blockEntity =
            plcGetBlockEntity(
                level,
                pos
            )


        if (!blockEntity) {

            console.error(
                '[PLC] PLC Reader has no BlockEntity.'
            )


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        /*
         * ------------------------------------------------------
         * INVENTORY
         * ------------------------------------------------------
         */

        var inventory =
            plcGetInventory(
                blockEntity
            )


        if (!inventory) {

            console.error(
                '[PLC] PLC Reader has no inventory attachment.'
            )


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        if (
            inventory.getSlots()
            < 1
        ) {

            console.error(
                '[PLC] PLC Reader inventory has zero slots.'
            )


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        /*
         * ------------------------------------------------------
         * HELD / STORED
         * ------------------------------------------------------
         */

        var held =
            player.getItemInHand(
                InteractionHandClass.MAIN_HAND
            )

        var stored =
            inventory.getStackInSlot(
                0
            )


        /* ======================================================
         * EMPTY HAND
         *
         * READER -> PLAYER
         * ======================================================
         */

        if (
            plcIsEmpty(
                held
            )
        ) {

            /*
             * Empty reader.
             */

            if (
                plcIsEmpty(
                    stored
                )
            ) {

                event.setCanceled(
                    true
                )

                event.setCancellationResult(
                    InteractionResultClass.SUCCESS
                )

                console.info(
                    '[PLC] Empty reader clicked with empty hand.'
                )

                return

            }


            /*
             * Only schematics are valid contents.
             */

            if (
                !plcIsSchematic(
                    stored
                )
            ) {

                console.error(
                    '[PLC] Unexpected item in PLC Reader: '
                    +
                    plcItemId(
                        stored
                    )
                )


                event.setCanceled(
                    true
                )

                event.setCancellationResult(
                    InteractionResultClass.SUCCESS
                )

                return

            }


            /*
             * Extract the complete stored schematic.
             */

            var extracted =
                plcExtractStored(
                    inventory,
                    stored.getCount()
                )


            if (
                plcIsEmpty(
                    extracted
                )
            ) {

                console.error(
                    '[PLC] Failed to extract schematic.'
                )


                event.setCanceled(
                    true
                )

                event.setCancellationResult(
                    InteractionResultClass.SUCCESS
                )

                return

            }


            /*
             * Hand now contains the extracted schematic.
             */

            player.setItemInHand(
                InteractionHandClass.MAIN_HAND,
                extracted
            )


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )


            console.info(
                '[PLC] Schematic returned to player.'
            )

            return

        }


        /* ======================================================
         * ONLY CREATE SCHEMATICS
         * ======================================================
         */

        if (
            !plcIsSchematic(
                held
            )
        ) {

            return

        }


        /*
         * Reader stores one schematic item.
         */

        var incoming =
            held.copy()

        incoming.setCount(
            1
        )


        /* ======================================================
         * EMPTY READER
         * ======================================================
         */

        if (
            plcIsEmpty(
                stored
            )
        ) {

            console.info(
                '[PLC] Inserting schematic into empty reader.'
            )


            var remainder =
                plcInsertStored(
                    inventory,
                    incoming
                )


            if (
                !remainder
            ) {

                console.error(
                    '[PLC] Inventory returned null during insertion.'
                )


                event.setCanceled(
                    true
                )

                event.setCancellationResult(
                    InteractionResultClass.SUCCESS
                )

                return

            }


            if (
                remainder.isEmpty()
            ) {

                /*
                 * Remove exactly one schematic from the hand.
                 */

                held.shrink(
                    1
                )


                if (
                    held.isEmpty()
                ) {

                    player.setItemInHand(
                        InteractionHandClass.MAIN_HAND,
                        ItemStackClass.EMPTY
                    )

                } else {

                    player.setItemInHand(
                        InteractionHandClass.MAIN_HAND,
                        held
                    )

                }


                console.info(
                    '[PLC] Schematic inserted successfully.'
                )

            } else {

                console.warn(
                    '[PLC] Schematic rejected by PLC Reader.'
                )

            }


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        /* ======================================================
         * OCCUPIED READER
         *
         * DEPOT-STYLE SWAP
         * ======================================================
         */

        if (
            !plcIsSchematic(
                stored
            )
        ) {

            console.error(
                '[PLC] Reader contains unexpected item; refusing swap.'
            )


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        /*
         * Preserve the outgoing schematic.
         */

        var oldSchematic =
            stored.copy()


        /*
         * Extract it.
         */

        var removed =
            plcExtractStored(
                inventory,
                stored.getCount()
            )


        if (
            plcIsEmpty(
                removed
            )
        ) {

            console.error(
                '[PLC] Failed to extract old schematic.'
            )


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        /*
         * Insert the incoming schematic.
         */

        var swapRemainder =
            plcInsertStored(
                inventory,
                incoming
            )


        if (
            !swapRemainder
            ||
            !swapRemainder.isEmpty()
        ) {

            console.error(
                '[PLC] New schematic rejected during swap.'
            )


            /*
             * Restore old schematic.
             */

            var restoreRemainder =
                plcInsertStored(
                    inventory,
                    oldSchematic
                )


            if (
                restoreRemainder
                &&
                !restoreRemainder.isEmpty()
            ) {

                console.error(
                    '[PLC] CRITICAL: failed to restore old schematic.'
                )

            }


            event.setCanceled(
                true
            )

            event.setCancellationResult(
                InteractionResultClass.SUCCESS
            )

            return

        }


        /*
         * Give old schematic to player.
         */

        player.setItemInHand(
            InteractionHandClass.MAIN_HAND,
            oldSchematic
        )


        event.setCanceled(
            true
        )

        event.setCancellationResult(
            InteractionResultClass.SUCCESS
        )


        console.info(
            '[PLC] Schematic swap completed.'
        )

    } catch (error) {

        console.error(
            '[PLC] RightClickBlock error: '
            +
            String(error)
        )

    }

}


/* ============================================================
 * FORGE EVENT CONSUMER
 * ============================================================
 *
 * Explicit Java Consumer prevents Rhino from passing an
 * untyped JavaScript function as Forge's event consumer.
 * ============================================================
 */

var plcRightClickConsumer =
    new ConsumerClass({

        accept: function(event) {

            plcHandleRightClick(
                event
            )

        }

    })


/* ============================================================
 * REGISTER EVENT
 * ============================================================
 */

try {

    MinecraftForgeClass.EVENT_BUS.addListener(
        EventPriorityClass.HIGHEST,
        false,
        RightClickBlockEventClass,
        plcRightClickConsumer
    )


    console.info(
        '[PLC] RightClickBlock handler registered at HIGHEST.'
    )

} catch (error) {

    console.error(
        '[PLC] Failed to register RightClickBlock handler: '
        +
        String(error)
    )

}


/* ============================================================
 * STARTUP REPORT
 * ============================================================
 */

console.info(
    '[PLC] ============================================'
)

console.info(
    '[PLC] PLC SCHEMATIC READER INTERACTION'
)

console.info(
    '[PLC] Block: ' + PLC_ID
)

console.info(
    '[PLC] Accepted item: ' + SCHEMATIC_ID
)

console.info(
    '[PLC] Interaction: Forge EVENT_BUS'
)

console.info(
    '[PLC] Event: PlayerInteractEvent.RightClickBlock'
)

console.info(
    '[PLC] Priority: HIGHEST'
)

console.info(
    '[PLC] Face restriction: TOP'
)

console.info(
    '[PLC] Empty hand: extract'
)

console.info(
    '[PLC] Empty reader: insert'
)

console.info(
    '[PLC] Occupied reader: swap'
)

console.info(
    '[PLC] ============================================'
)

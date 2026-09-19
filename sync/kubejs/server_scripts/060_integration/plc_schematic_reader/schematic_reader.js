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
 * Only TOP face interaction is accepted for PLC schematic
 * operations.
 *
 * IMPORTANT
 * ---------
 *
 * This handler is deliberately NON-GREEDY.
 *
 * It must NOT interfere with ordinary interactions on the PLC
 * block such as:
 *
 *     - CC wired modem
 *     - CC modem
 *     - hoes
 *     - Create tools
 *     - blocks
 *     - other items
 *
 * The handler only takes control when:
 *
 *     1. Target is the PLC reader
 *     2. Main hand is used
 *     3. Server side
 *     4. Held item is create:schematic
 *
 * OR:
 *
 *     4. Held hand is empty
 *
 * Everything else returns WITHOUT cancellation.
 *
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

function plcIsEmpty(
    stack
) {

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


/*
 * Resolve the Minecraft item registry ID.
 *
 * KubeJS exposes ItemStack.id.
 *
 * We retain getItem() as a compatibility fallback.
 */

function plcItemId(
    stack
) {

    try {

        if (
            plcIsEmpty(
                stack
            )
        ) {

            return null

        }


        /*
         * Preferred KubeJS path.
         */

        try {

            var id =
                stack.id

            if (
                id !== null
                &&
                id !== undefined
            ) {

                return String(
                    id
                )

            }

        } catch (ignored) {
        }


        /*
         * Java fallback.
         */

        try {

            var item =
                stack.getItem()

            if (
                item !== null
                &&
                item !== undefined
            ) {

                try {

                    var itemId =
                        item.id

                    if (
                        itemId !== null
                        &&
                        itemId !== undefined
                    ) {

                        return String(
                            itemId
                        )

                    }

                } catch (ignored) {
                }

            }

        } catch (ignored) {
        }


    } catch (error) {

    }


    return null

}


/*
 * Exact Create schematic test.
 */

function plcIsSchematic(
    stack
) {

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
         * Fallback registry string.
         */

        try {

            if (
                String(
                    state.getBlock()
                )
                ===
                'Block{' + PLC_ID + '}'
            ) {

                return true

            }

        } catch (ignored) {
        }


        return false

    } catch (error) {

        return false

    }

}


/* ============================================================
 * BLOCK ENTITY
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
 * INVENTORY
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
 * INVENTORY HELPERS
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
 * CANCEL HELPER
 * ============================================================
 *
 * Centralized so every handled PLC operation returns the
 * same Forge result.
 * ============================================================
 */

function plcCancelSuccess(
    event
) {

    try {

        event.setCanceled(
            true
        )

        event.setCancellationResult(
            InteractionResultClass.SUCCESS
        )

    } catch (error) {

        console.error(
            '[PLC] Failed to cancel interaction: '
            +
            String(error)
        )

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
         * ====================================================
         * 1. PLAYER
         * ====================================================
         */

        var player =
            event.getEntity()


        if (!player) {

            return

        }


        /*
         * ====================================================
         * 2. MAIN HAND ONLY
         * ====================================================
         *
         * Off-hand interaction is never consumed.
         * This is especially important for normal Minecraft/
         * ComputerCraft interactions.
         */

        if (
            event.getHand()
            !==
            InteractionHandClass.MAIN_HAND
        ) {

            return

        }


        /*
         * ====================================================
         * 3. LEVEL / POSITION
         * ====================================================
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
         * ====================================================
         * 4. TARGET BLOCK
         * ====================================================
         *
         * If this isn't our PLC reader, the handler is completely
         * passive.
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
         * ====================================================
         * 5. SERVER ONLY
         * ====================================================
         *
         * Do this before touching the block entity.
         */

        if (
            level.isClientSide()
        ) {

            return

        }


        /*
         * ====================================================
         * 6. READ PLAYER HAND
         * ====================================================
         */

        var held =
            player.getItemInHand(
                InteractionHandClass.MAIN_HAND
            )


        /*
         * ====================================================
         * 7. IMPORTANT INTERACTION FILTER
         * ====================================================
         *
         * ONLY THESE ARE PLC OPERATIONS:
         *
         *     empty hand
         *     create:schematic
         *
         * Everything else MUST return untouched.
         *
         * This is what allows a wired modem to be attached to
         * the reader and allows other tools/items to interact
         * normally.
         * ====================================================
         */

        var handIsEmpty =
            plcIsEmpty(
                held
            )

        var handIsSchematic =
            plcIsSchematic(
                held
            )


        if (
            !handIsEmpty
            &&
            !handIsSchematic
        ) {

            /*
             * NOT a PLC operation.
             *
             * DO NOT:
             *
             *     lookup block entity
             *     lookup inventory
             *     cancel event
             *     inspect inventory
             *     modify anything
             *
             * Simply return and allow the normal Minecraft /
             * ComputerCraft / Create interaction to continue.
             */

            return

        }


        /*
         * ====================================================
         * 8. TOP FACE ONLY
         * ====================================================
         *
         * This check now occurs AFTER the held-item filter.
         *
         * Therefore:
         *
         *     modem on side
         *     modem on top
         *     hoe on top
         *     wrench on side
         *     etc.
         *
         * are not intercepted.
         *
         * Only a valid PLC operation is subject to the TOP
         * restriction.
         */

        if (
            event.getFace()
            !==
            DirectionClass.UP
        ) {

            return

        }


        /*
         * ====================================================
         * 9. BLOCK ENTITY
         * ====================================================
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


            plcCancelSuccess(
                event
            )

            return

        }


        /*
         * ====================================================
         * 10. INVENTORY
         * ====================================================
         */

        var inventory =
            plcGetInventory(
                blockEntity
            )


        if (!inventory) {

            console.error(
                '[PLC] PLC Reader has no inventory attachment.'
            )


            plcCancelSuccess(
                event
            )

            return

        }


        /*
         * ====================================================
         * 11. SLOT CHECK
         * ====================================================
         */

        if (
            inventory.getSlots()
            < 1
        ) {

            console.error(
                '[PLC] PLC Reader inventory has zero slots.'
            )


            plcCancelSuccess(
                event
            )

            return

        }


        /*
         * ====================================================
         * 12. CURRENT STORED ITEM
         * ====================================================
         */

        var stored =
            inventory.getStackInSlot(
                0
            )


        /* ====================================================
         *
         * EMPTY HAND
         *
         * READER -> PLAYER
         *
         * ====================================================
         */

        if (
            handIsEmpty
        ) {

            /*
             * ------------------------------------------------
             * EMPTY READER
             * ------------------------------------------------
             */

            if (
                plcIsEmpty(
                    stored
                )
            ) {

                /*
                 * This is still a valid PLC interaction:
                 *
                 * empty hand + PLC reader.
                 *
                 * We consume it so the reader doesn't trigger
                 * unrelated normal block interaction behavior.
                 */

                plcCancelSuccess(
                    event
                )


                console.info(
                    '[PLC] Empty reader clicked with empty hand.'
                )

                return

            }


            /*
             * ------------------------------------------------
             * VALID STORED ITEM?
             * ------------------------------------------------
             *
             * Reader is intended to contain only schematics.
             * Refuse unexpected contents safely.
             */

            if (
                !plcIsSchematic(
                    stored
                )
            ) {

                console.error(
                    '[PLC] Unexpected item in PLC Reader: '
                    +
                    String(
                        plcItemId(
                            stored
                        )
                    )
                )


                plcCancelSuccess(
                    event
                )

                return

            }


            /*
             * ------------------------------------------------
             * EXTRACT COMPLETE SCHEMATIC
             * ------------------------------------------------
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


                plcCancelSuccess(
                    event
                )

                return

            }


            /*
             * ------------------------------------------------
             * PUT SCHEMATIC INTO PLAYER HAND
             * ------------------------------------------------
             */

            player.setItemInHand(
                InteractionHandClass.MAIN_HAND,
                extracted
            )


            plcCancelSuccess(
                event
            )


            console.info(
                '[PLC] Schematic returned to player.'
            )

            return

        }


        /* ====================================================
         *
         * SCHEMATIC IN HAND
         *
         * ====================================================
         *
         * At this point the earlier filter guarantees:
         *
         *     handIsSchematic == true
         */

        if (
            !handIsSchematic
        ) {

            /*
             * Defensive guard.
             *
             * Should be unreachable because of the interaction
             * filter above.
             */

            return

        }


        /*
         * Reader stores exactly one schematic item.
         */

        var incoming =
            held.copy()


        incoming.setCount(
            1
        )


        /* ====================================================
         *
         * EMPTY READER
         *
         * PLAYER -> READER
         *
         * ====================================================
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


            /*
             * Inventory failure.
             */

            if (
                !remainder
            ) {

                console.error(
                    '[PLC] Inventory returned null during insertion.'
                )


                plcCancelSuccess(
                    event
                )

                return

            }


            /*
             * Successful insertion.
             */

            if (
                remainder.isEmpty()
            ) {

                /*
                 * Remove exactly one schematic from the player's
                 * hand.
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

                /*
                 * Inventory rejected the item.
                 */

                console.warn(
                    '[PLC] Schematic rejected by PLC Reader.'
                )

            }


            plcCancelSuccess(
                event
            )

            return

        }


        /* ====================================================
         *
         * OCCUPIED READER
         *
         * DEPOT-STYLE SWAP
         *
         * ====================================================
         */

        if (
            !plcIsSchematic(
                stored
            )
        ) {

            /*
             * Reader contains something unexpected.
             *
             * Since the player IS holding a schematic, this IS
             * a PLC operation, so safely consume the interaction
             * rather than allowing an accidental insertion.
             */

            console.error(
                '[PLC] Reader contains unexpected item; refusing swap.'
            )


            plcCancelSuccess(
                event
            )

            return

        }


        /*
         * ----------------------------------------------------
         * PRESERVE OLD SCHEMATIC
         * ----------------------------------------------------
         */

        var oldSchematic =
            stored.copy()


        /*
         * ----------------------------------------------------
         * EXTRACT OLD SCHEMATIC
         * ----------------------------------------------------
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


            plcCancelSuccess(
                event
            )

            return

        }


        /*
         * ----------------------------------------------------
         * INSERT NEW SCHEMATIC
         * ----------------------------------------------------
         */

        var swapRemainder =
            plcInsertStored(
                inventory,
                incoming
            )


        /*
         * ----------------------------------------------------
         * INSERTION FAILED
         * ----------------------------------------------------
         */

        if (
            !swapRemainder
            ||
            !swapRemainder.isEmpty()
        ) {

            console.error(
                '[PLC] New schematic rejected during swap.'
            )


            /*
             * Restore the old schematic.
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


            plcCancelSuccess(
                event
            )

            return

        }


        /*
         * ----------------------------------------------------
         * GIVE OLD SCHEMATIC TO PLAYER
         * ----------------------------------------------------
         */

        player.setItemInHand(
            InteractionHandClass.MAIN_HAND,
            oldSchematic
        )


        plcCancelSuccess(
            event
        )


        console.info(
            '[PLC] Schematic swap completed.'
        )

    } catch (error) {

        /*
         * Defensive outer boundary.
         *
         * Never allow an exception to propagate into the Forge
         * EventBus.
         */

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
 * Explicit Java Consumer prevents Rhino from having to adapt
 * an untyped JavaScript function directly to the Forge listener.
 * ============================================================
 */

var plcRightClickConsumer =
    new ConsumerClass({

        accept:
            function(
                event
            ) {

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
    '[PLC] Server side only'
)

console.info(
    '[PLC] Main hand only'
)

console.info(
    '[PLC] Target block: PLC Reader only'
)

console.info(
    '[PLC] PLC operation: EMPTY HAND or CREATE SCHEMATIC'
)

console.info(
    '[PLC] Non-schematic items: PASS THROUGH'
)

console.info(
    '[PLC] Wired modem: PASS THROUGH'
)

console.info(
    '[PLC] Hoe/tool/block interaction: PASS THROUGH'
)

console.info(
    '[PLC] Face restriction: TOP for PLC operations'
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

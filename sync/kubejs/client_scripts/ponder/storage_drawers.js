Ponder.registry((event) => {
    event
        .create("storagedrawers:oak_1")
        .scene(
            "basic_drawer",
            "Using a Storage Drawer",
            (scene, util) => {
                const drawer = [2, 1, 2];
                const drawerFace = util.vector.blockSurface(drawer, "north");
                const drawerCenter = util.vector.centerOf(drawer);

                scene.showBasePlate();
                scene.world.setBlocks(drawer, "storagedrawers:oak_1", true);
                scene.world.showSection(drawer, "down");
                scene.idle(20);

                // ---------------------------------------------------------
                // What is this?
                // ---------------------------------------------------------

                scene.overlay
                    .showText(80)
                    .text(
                        "A Storage Drawer stores one type of item directly inside the block."
                    )
                    .pointAt(drawerCenter)
                    .placeNearTarget();

                scene.idle(80);

                // ---------------------------------------------------------
                // Insert one item
                // ---------------------------------------------------------

                scene.overlay
                    .showText(50)
                    .text("Right-click the front to insert items.")
                    .pointAt(drawerFace)
                    .placeNearTarget();

                scene.showControls(40, drawerFace, "left")
                    .rightClick()
                    .withItem("minecraft:cobblestone");

                scene.idle(20);

                const firstItem = scene.world.createItemEntity(
                    [1.0, 1.5, 2.5],
                    [0.06, 0.04, 0.0],
                    "minecraft:cobblestone"
                );

                scene.idle(20);

                scene.world.removeEntity(firstItem);

                scene.idle(20);

                scene.overlay
                    .showText(50)
                    .text(
                        "The first item determines what this drawer stores."
                    )
                    .pointAt(drawerCenter)
                    .placeNearTarget();

                scene.idle(50);

                // ---------------------------------------------------------
                // Double-right-click
                // ---------------------------------------------------------

                scene.overlay
                    .showText(60)
                    .text(
                        "Double-right-click to deposit matching items from your inventory."
                    )
                    .pointAt(drawerFace)
                    .placeNearTarget();

                scene.showControls(50, drawerFace, "left")
                    .rightClick()
                    .withItem("minecraft:cobblestone");

                scene.idle(10);

                scene.showControls(30, drawerFace, "left")
                    .rightClick()
                    .withItem("minecraft:cobblestone");

                scene.idle(15);

                const stackOne = scene.world.createItemEntity(
                    [0.8, 1.4, 2.2],
                    [0.08, 0.03, 0.02],
                    "minecraft:cobblestone"
                );

                const stackTwo = scene.world.createItemEntity(
                    [0.8, 1.6, 2.5],
                    [0.08, 0.01, 0.0],
                    "minecraft:cobblestone"
                );

                const stackThree = scene.world.createItemEntity(
                    [0.8, 1.8, 2.8],
                    [0.08, 0.04, -0.02],
                    "minecraft:cobblestone"
                );

                scene.idle(20);

                scene.world.removeEntity(stackOne);
                scene.world.removeEntity(stackTwo);
                scene.world.removeEntity(stackThree);

                scene.overlay
                    .showText(50)
                    .text("Only items matching the drawer are deposited.")
                    .pointAt(drawerCenter)
                    .placeNearTarget();

                scene.idle(50);

                // ---------------------------------------------------------
                // Retrieve one item
                // ---------------------------------------------------------

                scene.overlay
                    .showText(50)
                    .text("Left-click the front to retrieve one item.")
                    .pointAt(drawerFace)
                    .placeNearTarget();

                scene.showControls(40, drawerFace, "left")
                    .leftClick();

                scene.idle(15);

                const retrievedItem = scene.world.createItemEntity(
                    [2.5, 1.4, 1.7],
                    [0.0, 0.04, -0.08],
                    "minecraft:cobblestone"
                );

                scene.idle(25);

                scene.world.removeEntity(retrievedItem);

                scene.idle(20);

                // ---------------------------------------------------------
                // Retrieve a stack
                // ---------------------------------------------------------

                scene.overlay
                    .showText(60)
                    .text("Shift + left-click removes one full stack.")
                    .pointAt(drawerFace)
                    .placeNearTarget();

                scene.showControls(50, drawerFace, "left")
                    .leftClick()
                    .whileSneaking();

                scene.idle(15);

                const retrievedStack = scene.world.createItemEntity(
                    [2.5, 1.5, 1.7],
                    [0.0, 0.05, -0.08],
                    "64x minecraft:cobblestone"
                );

                scene.idle(25);

                scene.world.removeEntity(retrievedStack);

                scene.overlay
                    .showText(70)
                    .text(
                        "The amount removed is limited by Minecraft's normal stack size."
                    )
                    .pointAt(drawerFace)
                    .placeNearTarget();

                scene.idle(70);

                scene.overlay
                    .showText(60)
                    .text(
                        "Tools stack to 1, snowballs to 16, and most blocks to 64."
                    )
                    .pointAt(drawerCenter)
                    .placeNearTarget();

                scene.idle(60);

                // ---------------------------------------------------------
                // Breaking the drawer
                // ---------------------------------------------------------

                scene.overlay
                    .showText(70)
                    .text(
                        "To break the drawer, use the correct tool on any side other than the front."
                    )
                    .pointAt(drawerCenter)
                    .placeNearTarget();

                scene.idle(30);

                const side = util.vector.blockSurface(drawer, "west");

                scene.showControls(50, side, "right")
                    .leftClick()
                    .withItem("minecraft:stone_pickaxe");

                scene.idle(25);

                scene.world.destroyBlock(drawer);
                scene.idle(30);

                scene.overlay
                    .showText(60)
                    .text("The drawer can now be picked up and moved.")
                    .pointAt(drawerCenter)
                    .placeNearTarget();

                scene.idle(60);
            }
        );
});

ServerEvents.recipes(event => {
    // Tag: all modded stripped logs (same as CT)
    const logs = '#create:modded_stripped_wood';

    // Recipe pairs: [held item, output]
    const recipes = [
        ['create:andesite_alloy', 'create:andesite_casing'],
        ['create:brass_ingot', 'create:brass_casing']
    ];

    recipes.forEach((pair, index) => {
        event.recipes.create.deploying(
            pair[1],                   // output
            [logs, pair[0]]            // [processed item, held item]
        )
        .keepHeldItem()                // no arguments – toggles "keep held item"
        .id(`kubejs:deploy_log_${index}`);
    });

    // Smooth Stone via Deployer (stone + sandpaper)
    event.recipes.create.deploying(
        'minecraft:smooth_stone',
        ['minecraft:stone', '#create:sandpaper']
    )
     .id('kubejs:smooth_stone_deploy');
});
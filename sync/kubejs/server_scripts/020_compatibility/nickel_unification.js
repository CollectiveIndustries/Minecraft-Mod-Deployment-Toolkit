// Nickel Unification (TFMG → Immersive Engineering)
// Removes all TFMG nickel items and replaces them with IE counterparts

ServerEvents.recipes(event => {

    // ----- Remove all recipes that output TFMG nickel items -----
    event.remove({ output: 'tfmg:nickel_ingot' });
    event.remove({ output: 'tfmg:nickel_dust' });
    event.remove({ output: 'tfmg:nickel_ore' });
    event.remove({ output: 'tfmg:raw_nickel' });

    // ----- Add shapeless conversions: TFMG → IE (unified) -----
    event.shapeless('immersiveengineering:ingot_nickel', ['tfmg:nickel_ingot'])
        .id('kubejs:unify_nickel_ingot');

    event.shapeless('immersiveengineering:dust_nickel', ['tfmg:nickel_dust'])
        .id('kubejs:unify_nickel_dust');

    // ----- Add smelting: raw nickel → IE ingot (0.7 XP) -----
    event.smelting('immersiveengineering:ingot_nickel', 'tfmg:raw_nickel')
        .xp(0.7)
        .id('kubejs:unify_nickel_smelting');

    // ----- Replace all occurrences in existing recipes -----
    // Inputs (any TFMG nickel item used as ingredient)
    event.replaceInput({}, 'tfmg:nickel_ingot', 'immersiveengineering:ingot_nickel');
    event.replaceInput({}, 'tfmg:nickel_dust', 'immersiveengineering:dust_nickel');
    event.replaceInput({}, 'tfmg:nickel_ore', 'immersiveengineering:ore_nickel');
    event.replaceInput({}, 'tfmg:raw_nickel', 'immersiveengineering:raw_nickel');

    // Outputs (any recipe that produces TFMG nickel → produces IE instead)
    event.replaceOutput({}, 'tfmg:nickel_ingot', 'immersiveengineering:ingot_nickel');
    event.replaceOutput({}, 'tfmg:nickel_dust', 'immersiveengineering:dust_nickel');
    event.replaceOutput({}, 'tfmg:nickel_ore', 'immersiveengineering:ore_nickel');
    event.replaceOutput({}, 'tfmg:raw_nickel', 'immersiveengineering:raw_nickel');
});
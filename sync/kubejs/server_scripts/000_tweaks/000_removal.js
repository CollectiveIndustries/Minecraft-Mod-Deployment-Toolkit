ServerEvents.recipes(event => {

  // Remove crafting table chain (original)
  event.remove({ output: 'minecraft:chain' });

  // Remove all Create splashing recipes that use sand as input
  event.remove({ type: 'create:splashing', input: 'minecraft:sand' });

  // Remove the sequenced assembly recipe that outputs Create tracks
  event.remove({ type: 'create:sequenced_assembly', output: 'create:track' });

  // Remove all vanilla crafting table recipes that output createaddition:spool
  event.remove({ output: 'createaddition:spool' });

  // ----- Remove Smooth Stone from furnace (smelting) -----
  event.remove({ type: 'minecraft:smelting', output: 'minecraft:smooth_stone' });

  // ----- Remove Smooth Stone from stonecutter (we'll use deployer instead) -----
  event.remove({ type: 'minecraft:stonecutting', output: 'minecraft:smooth_stone' });

});
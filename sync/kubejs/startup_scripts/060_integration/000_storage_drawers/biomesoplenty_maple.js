// kubejs/startup_scripts/060_integration/000_storage_drawers/biomesoplenty_maple.js

const ResourceLocation = Java.loadClass('net.minecraft.resources.ResourceLocation');
const VariantData = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants$VariantData'
);
const ModBlockVariants = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants'
);
const ExtraBlocks = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawersextra.core.ModBlocks'
);
const ExtraItems = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawersextra.core.ModItems'
);

console.info('[SD Maple] loading');

const material = new ResourceLocation(
    'storagedrawersextra',
    'biomesoplenty_maple'
);

const data = new VariantData(material);

ModBlockVariants.registerVariant(
    ExtraBlocks.BLOCK_REGISTER,
    data
);

ModBlockVariants.registerVariantItem(
    ExtraItems.ITEM_REGISTER,
    data
);

console.info('[SD Maple] registered blocks and items');

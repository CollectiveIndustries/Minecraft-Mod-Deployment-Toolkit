// kubejs/startup_scripts/dump_attributes.js

// Import required Java classes
const ForgeRegistries = Java.loadClass('net.minecraftforge.registries.ForgeRegistries');
const RangedAttribute = Java.loadClass('net.minecraft.world.entity.ai.attributes.RangedAttribute');
const File = Java.loadClass('java.io.File');
const FileWriter = Java.loadClass('java.io.FileWriter');
const PrintWriter = Java.loadClass('java.io.PrintWriter');

// Get the attribute registry
const registry = ForgeRegistries.ATTRIBUTES;

// Set up output file (saved in the kubejs folder)
const outputFile = new File('./kubejs/dumped_attributes.txt');
const writer = new PrintWriter(new FileWriter(outputFile));

console.info('===== Dumping Registered Attributes to file and console =====');
writer.println('===== Registered Attributes =====');

// Iterate over all entries
registry.getEntries().forEach(entry => {
    const id = entry.getKey().toString();           // e.g. "minecraft:generic.max_health"
    const attr = entry.getValue();
    const className = attr.getClass().getName();

    let details = `ID: ${id}\nClass: ${className}`;

    // If it's a RangedAttribute, extract min/max/default/description
    if (attr instanceof RangedAttribute) {
        const defaultVal = attr.getDefaultValue();
        const minVal = attr.getMinValue();
        const maxVal = attr.getMaxValue();
        const descriptionId = attr.getDescriptionId();
        details += `\nDefault value: ${defaultVal}`;
        details += `\nMin value: ${minVal}`;
        details += `\nMax value: ${maxVal}`;
        details += `\nDescription ID: ${descriptionId}`;
    } else {
        details += '\n(Not a RangedAttribute)';
    }

    // Registry name (ResourceLocation)
    const registryName = entry.getKey();
    details += `\nRegistry Name: ${registryName}`;

    // Output to console and file
    console.info(details);
    writer.println(details);
    writer.println('---------------------------');
});

writer.close();
console.info('===== Dump complete. Check kubejs/dumped_attributes.txt =====');
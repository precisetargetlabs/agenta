import {SchemaType} from "../baseTypes"
import type {SchemaProperty, AnyOfSchema, ObjectSchema} from "../openApiSchema"
import {isSchema, hasType} from "../utilities/schema"

export interface ExtractedSchema {
    schema: SchemaProperty
    parentTitle?: string
    parentDescription?: string
    isNullable: boolean
}

function createPrimitiveSchema(
    parentMetadata: Omit<AnyOfSchema, "anyOf">,
    selectedSchema: SchemaProperty,
    schemaType: Exclude<SchemaType, "object" | "array" | "compound">,
): SchemaProperty {
    const choices =
        ("choices" in selectedSchema && selectedSchema.choices) ||
        ("choices" in parentMetadata && parentMetadata.choices) ||
        undefined

    const processedChoices =
        choices &&
        (Array.isArray(choices)
            ? choices.map((c) => ({label: String(c.label), value: String(c.value)}))
            : Object.fromEntries(Object.entries(choices).map(([k, v]) => [k, v.map(String)])))

    return {
        ...parentMetadata,
        ...selectedSchema,
        type: schemaType,
        title: selectedSchema.title || parentMetadata.title,
        description: selectedSchema.description || parentMetadata.description,
        default: selectedSchema.default ?? parentMetadata.default,
        enum: selectedSchema.enum as string[] | undefined,
        // @ts-ignore
        choices: processedChoices,
    }
}

function combineSchemas(
    parentMetadata: Omit<AnyOfSchema, "anyOf">,
    selectedSchema: SchemaProperty,
): SchemaProperty {
    if ("type" in selectedSchema) {
        const schemaType = selectedSchema.type
        switch (schemaType) {
            case "array":
                return {
                    ...parentMetadata,
                    ...selectedSchema,
                    type: "array",
                    title: selectedSchema.title || parentMetadata.title,
                    description: selectedSchema.description || parentMetadata.description,
                    default: selectedSchema.default ?? parentMetadata.default,
                }
            case "object":
                return {
                    ...parentMetadata,
                    ...selectedSchema,
                    type: "object",
                    title: selectedSchema.title || parentMetadata.title,
                    description: selectedSchema.description || parentMetadata.description,
                    default: selectedSchema.default ?? parentMetadata.default,
                }
            default:
                // @ts-ignore
                return createPrimitiveSchema(parentMetadata, selectedSchema, schemaType)
        }
    }

    // Handle anyOf case
    if ("anyOf" in selectedSchema) {
        return {
            ...parentMetadata,
            ...selectedSchema,
        }
    }

    throw new Error("Invalid schema type")
}

/**
 * Extracts the most appropriate schema from an AnyOfSchema.
 * Prefers primitive, array, and object schemas in that order.
 * @param anyOfSchema - The AnyOfSchema to extract from.
 * @returns An ExtractedSchema object containing the chosen schema and metadata.
 *
 * An AnyOfSchema is a type of schema used in OpenAPI specifications to represent a property
 * that can conform to one of several different schemas. It is particularly useful when you
 * want to define a property that can have multiple possible types or structures. The `anyOf`
 * keyword is used to specify an array of schemas, and the property must validate against at
 * least one of these schemas.
 *
 * Example:
 * {
 *   "type": "object",
 *   "properties": {
 *     "exampleProperty": {
 *       "anyOf": [
 *         { "type": "string" },
 *         { "type": "integer" }
 *       ]
 *     }
 *   }
 * }
 *
 * In this example, `exampleProperty` can be either a string or an integer.
 */
export function extractFromAnyOf(anyOfSchema: AnyOfSchema): ExtractedSchema {
    const nonNullSchemas = anyOfSchema.anyOf.filter(
        (schema) => hasType(schema) && schema.type !== "null",
    )

    const isNullable = anyOfSchema.anyOf.some((schema) => hasType(schema) && schema.type === "null")

    if (nonNullSchemas.length === 0) {
        throw new Error("No valid non-null schema found in anyOf")
    }

    // Find the most appropriate schema using isSchema object
    const selectedSchema =
        nonNullSchemas.find(isSchema.primitive) ||
        nonNullSchemas.find(isSchema.array) ||
        nonNullSchemas.find(isSchema.object) ||
        nonNullSchemas[0]

    // Type assertion to ensure schema compatibility
    const typedSelectedSchema: SchemaProperty = {
        ...selectedSchema,
        enum: selectedSchema.enum as string[] | undefined,
    }

    // Combine the parent metadata with the selected schema
    const {anyOf, ...parentMetadata} = anyOfSchema
    const combinedSchema = combineSchemas(parentMetadata, typedSelectedSchema)

    return {
        schema: combinedSchema,
        isNullable,
    }
}

/**
 * Extracts schema information from an ObjectSchema.
 * @param objectSchema - The ObjectSchema to extract from.
 * @returns An ExtractedSchema object containing the schema and metadata.
 *
 * An ObjectSchema is a type of schema used in OpenAPI specifications to represent a JSON object.
 * It defines the properties of the object, each of which can have its own schema. ObjectSchemas
 * are useful for defining complex data structures with nested properties.
 *
 * Example:
 * {
 *   "type": "object",
 *   "properties": {
 *     "name": { "type": "string" },
 *     "age": { "type": "integer" }
 *   }
 * }
 *
 * In this example, the schema defines an object with two properties: `name` (a string) and `age` (an integer).
 */
export function extractFromObject(objectSchema: ObjectSchema): ExtractedSchema {
    return {
        schema: objectSchema,
        isNullable: false,
    }
}

/**
 * Type guard to check if a schema is an AnyOfSchema.
 * @param schema - The schema to check.
 * @returns True if the schema is an AnyOfSchema, false otherwise.
 */
function isAnyOfSchema(schema: SchemaProperty): schema is AnyOfSchema {
    return "anyOf" in schema
}

/**
 * Extracts schema information from a SchemaProperty.
 * Delegates to specific extractors based on schema type.
 * @param schema - The SchemaProperty to extract from.
 * @returns An ExtractedSchema object containing the schema and metadata.
 */
export function extractSchema(schema: SchemaProperty): ExtractedSchema {
    if (isAnyOfSchema(schema)) {
        return extractFromAnyOf(schema)
    }

    return {
        schema,
        isNullable: false,
    }
}
